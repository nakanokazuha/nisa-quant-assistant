"""Append-only recommendation records and point-in-time outcome evaluation."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record_recommendation(
    connection: sqlite3.Connection,
    candidate: dict[str, Any],
    *,
    data_cutoff: str,
    provider: str,
    template_version: str,
) -> int:
    """Store the original recommendation envelope without mutating it later."""
    cursor = connection.execute(
        """
        INSERT INTO recommendations(
            created_at, data_cutoff, provider, template_version, source_ids, instrument,
            label, metrics_json, reason, risk, horizon, invalidation
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _now(), data_cutoff, provider, template_version,
            json.dumps(candidate["source_ids"], sort_keys=True), candidate["instrument"],
            candidate["label"], json.dumps(candidate["metrics"], sort_keys=True),
            candidate["reason"], candidate["risk_counter_evidence"], candidate["horizon"],
            candidate["invalidation"],
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def evaluate_recommendation(
    connection: sqlite3.Connection,
    recommendation_id: int,
    *,
    evaluation_date: str,
    observed_price: float,
    benchmark_price: float,
) -> None:
    """Append a later outcome using the original recorded prices as its baseline."""
    row = connection.execute(
        "SELECT metrics_json FROM recommendations WHERE id = ?", (recommendation_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown recommendation: {recommendation_id}")
    metrics = json.loads(row[0])
    original_price = metrics.get("current_price")
    original_benchmark = metrics.get("benchmark_price")
    observed_return = (observed_price / original_price - 1) if original_price else None
    benchmark_return = (benchmark_price / original_benchmark - 1) if original_benchmark else None
    snapshot = {
        "evaluation_date": evaluation_date,
        "observed_price": observed_price,
        "benchmark_price": benchmark_price,
        "original_price": original_price,
        "original_benchmark_price": original_benchmark,
    }
    connection.execute(
        """
        INSERT INTO recommendation_outcomes(
            recommendation_id, evaluation_date, observed_price, benchmark_price,
            observed_return, benchmark_return, snapshot_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (recommendation_id, evaluation_date, observed_price, benchmark_price,
         observed_return, benchmark_return, json.dumps(snapshot, sort_keys=True)),
    )
    connection.commit()
