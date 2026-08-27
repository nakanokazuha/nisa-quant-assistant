"""Local source-record adapters; no network clients are included."""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def add_source_record(
    connection: sqlite3.Connection,
    *,
    source_name: str,
    source_identifier: str,
    retrieved_at: str,
    observation_date: str | None,
    instrument_identifier: str | None,
    field: str,
    value: str | None,
    unit: str | None,
    currency: str | None,
    freshness_status: str,
    citation_location: str,
) -> str:
    """Insert a cited fact and return its stable source ID."""
    identity = "|".join(
        str(part)
        for part in (
            source_name,
            source_identifier,
            observation_date,
            instrument_identifier,
            field,
            value,
            unit,
            currency,
            citation_location,
        )
    )
    source_id = f"SRC-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
    connection.execute(
        """
        INSERT OR IGNORE INTO source_records(
            id, source_name, source_url_or_identifier, retrieved_at, observation_date,
            instrument_identifier, field, value, unit, currency, freshness_status,
            citation_location
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            source_name,
            source_identifier,
            retrieved_at,
            observation_date,
            instrument_identifier,
            field,
            value,
            unit,
            currency,
            freshness_status,
            citation_location,
        ),
    )
    return source_id


@dataclass(frozen=True, slots=True)
class PriceFixtureResult:
    accepted_rows: int
    source_ids: tuple[str, ...]


def import_price_fixture(
    connection: sqlite3.Connection,
    path: Path,
    *,
    source_name: str,
) -> PriceFixtureResult:
    """Load a strict local price fixture, retaining every source observation."""
    required = {
        "identifier",
        "identifier_type",
        "observation_date",
        "price",
        "currency",
        "retrieved_at",
        "freshness_status",
        "citation_location",
        "benchmark",
    }
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if set(reader.fieldnames or ()) != required:
            raise ValueError("price fixture must use the documented exact columns")
        source_ids: list[str] = []
        for row_number, row in enumerate(reader, 2):
            identifier = row["identifier"]
            identifier_type = row["identifier_type"]
            instrument = connection.execute(
                "SELECT id FROM instruments WHERE identifier_type = ? AND identifier_value = ?",
                (identifier_type, identifier),
            ).fetchone()
            if instrument is None:
                instrument_id = connection.execute(
                    """
                    INSERT INTO instruments(identifier_type, identifier_value, display_name, currency)
                    VALUES (?, ?, ?, ?)
                    RETURNING id
                    """,
                    (identifier_type, identifier, identifier, row["currency"]),
                ).fetchone()[0]
            else:
                instrument_id = instrument[0]
            source_id = add_source_record(
                connection,
                source_name=source_name,
                source_identifier=f"{path.name}#row-{row_number}",
                retrieved_at=row["retrieved_at"],
                observation_date=row["observation_date"],
                instrument_identifier=identifier,
                        field="price" if row["benchmark"].strip() == "no" else "benchmark_price",
                value=row["price"],
                unit="price",
                currency=row["currency"],
                freshness_status=row["freshness_status"],
                citation_location=row["citation_location"],
            )
            source_ids.append(source_id)
            if row["benchmark"].strip() == "no":
                connection.execute(
                    "UPDATE positions SET latest_price = ?, price_date = ?, price_status = ? WHERE instrument_id = ? AND latest_price IS NULL",
                    (float(row["price"]), row["observation_date"], row["freshness_status"], instrument_id),
                )
        for instrument_id, in connection.execute(
            "SELECT DISTINCT instrument_id FROM positions"
        ).fetchall():
            price_count = connection.execute(
                """
                SELECT COUNT(DISTINCT value) FROM source_records sr
                JOIN instruments i ON i.identifier_value = sr.instrument_identifier
                WHERE sr.field = 'price' AND i.id = ?
                """,
                (instrument_id,),
            ).fetchone()[0]
            if price_count > 1:
                connection.execute(
                    "UPDATE positions SET price_status = 'conflicting' WHERE instrument_id = ?",
                    (instrument_id,),
                )
        connection.commit()
    return PriceFixtureResult(len(source_ids), tuple(source_ids))
