"""Deterministic portfolio calculations and visible data-quality statuses."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any


STALE_AFTER_DAYS = 30
RECOGNIZED_ACCOUNTS = {"NISA", "taxable", "general"}


def _instrument_label(connection: sqlite3.Connection, instrument_id: int) -> tuple[str, str]:
    row = connection.execute(
        "SELECT identifier_value, display_name FROM instruments WHERE id = ?", (instrument_id,)
    ).fetchone()
    yahoo = connection.execute(
        """
        SELECT i.identifier_value FROM instrument_links l
        JOIN instruments i ON i.id = l.to_instrument_id
        WHERE l.from_instrument_id = ? AND l.link_type = 'explicit_yahoo_alias'
        """,
        (instrument_id,),
    ).fetchone()
    return (yahoo[0] if yahoo else row[0], row[1])


def _warning(code: str, message: str, *, instrument: str | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "instrument": instrument}


def calculate_snapshot(
    connection: sqlite3.Connection,
    *,
    as_of: str,
) -> dict[str, Any]:
    """Calculate a JSON-compatible snapshot without any model or network calls."""
    as_of_date = date.fromisoformat(as_of)
    warnings: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT warning_code, message, row_number FROM data_warnings ORDER BY id"
    ):
        warnings.append(
            {"code": row[0], "message": row[1], "row_number": row[2]}
        )
    position_rows = connection.execute(
        """
        SELECT p.*, a.account_type FROM positions p
        JOIN accounts a ON a.id = p.account_id
        WHERE a.account_type IN ('NISA', 'taxable', 'general')
        ORDER BY a.account_type, p.instrument_id
        """
    ).fetchall()
    holdings: list[dict[str, Any]] = []
    market_value = 0.0
    cost_basis = 0.0
    for row in position_rows:
        instrument, name = _instrument_label(connection, row["instrument_id"])
        latest_price = row["latest_price"]
        value = latest_price * row["quantity"] if latest_price is not None else None
        if value is None:
            warnings.append(_warning("MISSING_PRICE", f"No accepted price for {instrument}", instrument=instrument))
        else:
            market_value += value
        if row["price_status"] == "stale":
            warnings.append(_warning("STALE_PRICE", f"Price for {instrument} is marked stale", instrument=instrument))
        if row["price_status"] == "conflicting":
            warnings.append(_warning("CONFLICTING_PRICE", f"Multiple source prices conflict for {instrument}", instrument=instrument))
        if row["price_date"]:
            age = (as_of_date - date.fromisoformat(row["price_date"])).days
            if age > STALE_AFTER_DAYS and row["price_status"] != "stale":
                warnings.append(_warning("STALE_PRICE", f"Price for {instrument} is {age} days old", instrument=instrument))
        raw_identifier = connection.execute(
            "SELECT identifier_value FROM instruments WHERE id = ?", (row["instrument_id"],)
        ).fetchone()[0]
        source_ids = [
            source[0]
            for source in connection.execute(
                """
                SELECT id FROM source_records
                WHERE field = 'price' AND instrument_identifier IN (?, ?)
                ORDER BY observation_date DESC, id
                """,
                (raw_identifier, instrument),
            )
        ]
        holdings.append(
            {
                "instrument": instrument,
                "display_name": name,
                "account": row["account_type"],
                "quantity": row["quantity"],
                "cost_basis": row["cost_basis"],
                "latest_price": latest_price,
                "market_value": value,
                "price_date": row["price_date"],
                "price_status": row["price_status"],
                "source_ids": source_ids,
            }
        )
        cost_basis += row["cost_basis"]
    contribution = connection.execute(
        "SELECT COALESCE(SUM(quantity * price + fee), 0) FROM transactions WHERE transaction_type = 'BUY'"
    ).fetchone()[0]
    distributions = connection.execute(
        "SELECT COALESCE(SUM(distribution), 0) FROM transactions WHERE transaction_type = 'DISTRIBUTION'"
    ).fetchone()[0]
    sources = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM source_records ORDER BY observation_date, id"
        )
    ]
    benchmark_rows = [source for source in sources if source["field"] == "benchmark_price"]
    return {
        "as_of": as_of,
        "data_cutoffs": {
            "portfolio": as_of,
            "prices": max((source["observation_date"] or "" for source in sources if source["field"] == "price"), default=None),
            "benchmark": max((source["observation_date"] or "" for source in benchmark_rows), default=None),
        },
        "portfolio": {
            "market_value": market_value if holdings and not any(h["market_value"] is None for h in holdings) else None,
            "cost_basis": cost_basis,
            "contributions": contribution,
            "distributions": distributions,
            "unrealized_pl": market_value - cost_basis if holdings and not any(h["market_value"] is None for h in holdings) else None,
            "holdings": holdings,
        },
        "benchmarks": [
            {
                "instrument": source["instrument_identifier"],
                "price": float(source["value"]),
                "observation_date": source["observation_date"],
                "source_id": source["id"],
            }
            for source in benchmark_rows
        ],
        "warnings": warnings,
        "sources": sources,
    }
