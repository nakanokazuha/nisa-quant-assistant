"""Transparent deterministic candidate screens."""

from __future__ import annotations

import sqlite3
from typing import Any

from .metrics import calculate_snapshot

LABELS = {
    "BUY CANDIDATE",
    "HOLD",
    "SELL / REDUCE CANDIDATE",
    "WATCH",
    "NO ACTION / INSUFFICIENT DATA",
}


def run_screens(
    connection: sqlite3.Connection,
    snapshot: dict[str, Any],
    *,
    as_of: str,
) -> list[dict[str, Any]]:
    """Rank holdings using transparent metrics and refuse unsafe directionality."""
    del connection, as_of
    candidates: list[dict[str, Any]] = []
    benchmark_price = snapshot["benchmarks"][0]["price"] if snapshot["benchmarks"] else None
    for holding in snapshot["portfolio"]["holdings"]:
        status = holding["price_status"]
        material_warning = status in {"stale", "conflicting", "unavailable"} or holding["latest_price"] is None
        if material_warning:
            label = "NO ACTION / INSUFFICIENT DATA" if status == "conflicting" else "WATCH"
            evidence = "insufficient"
        else:
            label = "WATCH"
            evidence = "limited"
        current_price = holding["latest_price"]
        metrics = {
            "quantity": holding["quantity"],
            "cost_basis": holding["cost_basis"],
            "current_price": current_price,
            "position_return_pct": (
                (current_price * holding["quantity"] - holding["cost_basis"]) / holding["cost_basis"] * 100
                if current_price is not None and holding["cost_basis"]
                else None
            ),
            "benchmark_price": benchmark_price,
            "price_date": holding["price_date"],
            "price_status": status,
        }
        candidates.append(
            {
                "instrument": holding["instrument"],
                "account": holding["account"],
                "label": label,
                "evidence_quality": evidence,
                "horizon": "long-term",
                "metrics": metrics,
                "reason": "Deterministic screen is limited to the accepted local price and ledger facts.",
                "risk_counter_evidence": "Price freshness or source agreement is not sufficient for a directional view.",
                "invalidation": "Reassess when a fresh, corroborated price history and required fundamentals are available.",
                "source_ids": holding["source_ids"],
                "manual_review": "Manual review required; no order can be placed by this tool.",
            }
        )
    return sorted(candidates, key=lambda candidate: candidate["instrument"])
