"""Transparent deterministic candidate screens for holdings and watchlists."""

from __future__ import annotations

import sqlite3
import math
from typing import Any

from .source_records import safe_add, safe_divide, safe_multiply


LABELS = {
    "BUY CANDIDATE",
    "HOLD",
    "SELL / REDUCE CANDIDATE",
    "WATCH",
    "NO ACTION / INSUFFICIENT DATA",
}


def _position_return(record: dict[str, Any]) -> float | None:
    current_price = record.get("latest_price")
    quantity = record.get("quantity")
    cost_basis = record.get("cost_basis")
    if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in (current_price, quantity, cost_basis)):
        return None
    if not cost_basis:
        return None
    market_value = safe_multiply(float(current_price), float(quantity))
    numerator = safe_add(market_value or math.inf, -float(cost_basis))
    ratio = safe_divide(numerator or math.inf, float(cost_basis))
    result = ratio * 100 if ratio is not None else None
    return result if result is not None and math.isfinite(result) else None


def _momentum(values: list[float]) -> float | None:
    if len(values) < 2 or not values[0]:
        return None
    ratio = safe_divide(values[-1], values[0])
    result = (ratio - 1) * 100 if ratio is not None else None
    return result if result is not None and math.isfinite(result) else None


def _candidate_from_record(
    record: dict[str, Any],
    *,
    account: str,
    ledger_source_ids: list[str],
    accepted_source_ids: set[str] | None = None,
) -> dict[str, Any]:
    values = [item["price"] for item in record.get("price_history", [])]
    current_price = record.get("latest_price")
    enough_history = len(values) >= 3
    moving_average_total = safe_add(*values[-3:]) if enough_history else None
    moving_average = safe_divide(moving_average_total, 3.0) if moving_average_total is not None else None
    material_warning = (
        record.get("price_status") in {"stale", "conflicting", "unavailable"}
        or current_price is None
        or record.get("metadata_status") == "unavailable"
    )
    label = "NO ACTION / INSUFFICIENT DATA" if record.get("price_status") == "conflicting" else "WATCH"
    evidence = "insufficient" if material_warning or not enough_history else "limited"
    if accepted_source_ids is None:
        accepted_source_ids = {
            *record.get("source_ids", []),
            *record.get("benchmark_source_ids", []),
            *record.get("distribution_source_ids", []),
            *record.get("ledger_source_ids", []),
        }
    source_ids = [
        source_id for source_id in dict.fromkeys(
            record.get("source_ids", [])
            + record.get("benchmark_source_ids", [])
            + record.get("distribution_source_ids", [])
        )
        if source_id in accepted_source_ids
    ]
    metrics = {
        "quantity": record.get("quantity", 0.0),
        "cost_basis": record.get("cost_basis", 0.0),
        "current_price": current_price,
        "position_return_pct": _position_return(record),
        "benchmark_price": record.get("benchmark_price"),
        "price_date": record.get("price_date"),
        "price_status": record.get("price_status"),
        "momentum_pct": _momentum(values),
        "moving_average_3": moving_average,
        "trend_context": (
            "above moving average" if moving_average is not None and current_price is not None and current_price > moving_average
            else "below moving average" if moving_average is not None and current_price is not None
            else "unavailable"
        ),
        "benchmark_return_pct": record.get("benchmark_return_pct"),
        "benchmark_relative_pct": record.get("benchmark_relative_pct"),
        "max_drawdown_pct": record.get("max_drawdown_pct"),
        "volatility_pct": record.get("volatility_pct"),
        "distribution_amount": record.get("distribution_amount"),
        "distribution_change_pct": record.get("distribution_change_pct"),
        "distribution_yield_pct": record.get("distribution_yield_pct"),
        "distribution_date": record.get("distribution_date"),
        "distribution_data_cutoff": record.get("distribution_data_cutoff"),
        "distribution_unit": record.get("distribution_unit"),
    }
    return {
        "instrument": record["identifier"],
        "identifier_type": record["identifier_type"],
        "identifier_value": record["identifier"],
        "display_name": record.get("display_name", record["identifier"]),
        "asset_type": record.get("asset_type", "security"),
        "market": record.get("market"),
        "currency": record.get("currency"),
        "benchmark": record.get("benchmark"),
        "benchmark_identifier_type": record.get("benchmark_identifier_type"),
        "benchmark_identifier_value": record.get("benchmark_identifier_value", record.get("benchmark")),
        "benchmark_instrument": record.get("benchmark_instrument"),
        "account": account,
        "label": label,
        "evidence_quality": evidence,
        "horizon": "long-term",
        "metrics": metrics,
        "reason": "Deterministic screen is limited to accepted local price and ledger/watchlist facts.",
        "risk_counter_evidence": "Price freshness or source agreement is not sufficient for a directional view.",
        "invalidation": "Reassess when a fresh, corroborated price history and required fundamentals are available.",
        "source_ids": source_ids,
        "ledger_source_ids": [source_id for source_id in ledger_source_ids if source_id in accepted_source_ids],
        "manual_review": "Manual review required; no order can be placed by this tool.",
    }
def run_screens(
    connection: sqlite3.Connection,
    snapshot: dict[str, Any],
    *,
    as_of: str,
) -> list[dict[str, Any]]:
    """Return deterministic, non-ordering candidates for holdings and watchlist items."""
    del as_of
    accepted_source_ids = {source["id"] for source in snapshot.get("sources", [])}
    candidates: list[dict[str, Any]] = []
    for holding in snapshot["portfolio"]["holdings"]:
        candidates.append(_candidate_from_record(
            holding, account=holding["account"], ledger_source_ids=holding.get("ledger_source_ids", []),
            accepted_source_ids=accepted_source_ids,
        ))
    for item in snapshot.get("watchlist", []):
        candidates.append(_candidate_from_record(
            item, account="watchlist", ledger_source_ids=[], accepted_source_ids=accepted_source_ids,
        ))
    return sorted(candidates, key=lambda candidate: (candidate["instrument"], candidate["identifier_type"]))
