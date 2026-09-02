"""Local source-record adapters; no network clients are included.

Source identity includes retrieval/parser metadata so a refreshed observation
is retained for audit instead of being hidden by a file hash or old row ID.
Price and benchmark facts must be finite and strictly positive; distribution
amounts are finite and non-negative before they can be used by metrics. Numeric
fact fields use a closed field/unit contract: market and trade prices use
``price``, distributions use ``per_unit`` or ``total_cash``, and cash movements
use ``total_cash``. Security facts require typed identity; cash movements are
the only identity-free fact. Derived arithmetic helpers fail closed on overflow.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PARSER_VERSION = "price-fixture-v3"
DISTRIBUTION_PARSER_VERSION = "distribution-fixture-v1"
IDENTIFIER_TYPES = {"jpx_code", "yahoo_symbol", "isin", "other"}
NUMERIC_FIELDS = {"price", "benchmark_price", "buy", "sell", "distribution", "cash_movement"}
FRESHNESS_STATUSES = frozenset({"current", "observed", "stale", "conflicting", "unavailable"})
USABLE_FRESHNESS_STATUSES = frozenset({"current", "observed"})
REQUIRED_FACT_FIELDS = frozenset({"price", "benchmark_price", "distribution", "buy", "sell", "cash_movement"})
SOURCE_FACT_UNITS = {
    "price": frozenset({"price"}),
    "benchmark_price": frozenset({"price"}),
    "buy": frozenset({"price"}),
    "sell": frozenset({"price"}),
    "distribution": frozenset({"per_unit", "total_cash"}),
    "cash_movement": frozenset({"total_cash"}),
}
SUPPORTED_FACT_FIELDS = frozenset(SOURCE_FACT_UNITS)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_retrieved_at(value: str) -> datetime:
    """Parse a retrieval timestamp and return its UTC-aware instant.

    Legacy naïve timestamps are interpreted as UTC so historical local rows
    remain readable under the documented UTC policy.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("source retrieval dates must be ISO timestamps") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_retrieved_at(value: str) -> str:
    """Return a canonical UTC representation of a retrieval timestamp."""
    return parse_retrieved_at(value).isoformat()


def source_fact_chronology_is_valid(source: Any) -> bool:
    """Return whether a persisted fact was retrieved on or after observation."""
    try:
        observation_value = source["observation_date"]
    except (IndexError, KeyError):
        observation_value = source["source_observation_date"]
    try:
        retrieved_value = source["retrieved_at"]
    except (IndexError, KeyError):
        retrieved_value = source["source_retrieved_at"]
    if not isinstance(observation_value, str):
        return False
    try:
        observation = date.fromisoformat(observation_value)
        retrieved = parse_retrieved_at(retrieved_value)
    except (AttributeError, TypeError, ValueError):
        return False
    return observation.isoformat() == observation_value and retrieved.date() >= observation


def safe_add(*values: float) -> float | None:
    """Add finite values, returning unavailable when the result overflows."""
    result = 0.0
    for value in values:
        result += value
        if not math.isfinite(result):
            return None
    return result


def safe_multiply(left: float, right: float) -> float | None:
    """Multiply finite values, returning unavailable when the result overflows."""
    result = left * right
    return result if math.isfinite(result) else None


def safe_divide(numerator: float, denominator: float) -> float | None:
    """Divide finite values, returning unavailable for invalid derived results."""
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator == 0:
        return None
    result = numerator / denominator
    return result if math.isfinite(result) else None


def _source_column(source: Any, name: str, alias: str) -> Any:
    """Read a source column from either a source row or a joined ledger row."""
    try:
        return source[name]
    except (IndexError, KeyError):
        return source[alias]


def source_fact_is_usable(source: Any) -> bool:
    """Return whether a numeric source row has complete usable provenance."""
    if not source_fact_contract_is_valid(source):
        return False
    return source["freshness_status"] in USABLE_FRESHNESS_STATUSES


def source_fact_contract_is_valid(source: Any) -> bool:
    """Return whether a source row has a supported field and valid fact shape."""
    required_text_fields = (
        "source_name", "source_url_or_identifier", "retrieved_at", "citation_location",
        "parser_version", "field", "freshness_status", "unit", "currency",
        "observation_date",
    )
    try:
        metadata = {
            field: _source_column(source, field, f"source_{field}")
            for field in required_text_fields
        }
        if any(
            not isinstance(metadata[field], str) or not metadata[field].strip()
            for field in required_text_fields
        ):
            return False
    except (IndexError, KeyError):
        return False
    field = source["field"]
    if field not in SUPPORTED_FACT_FIELDS:
        return False
    if source["freshness_status"] not in FRESHNESS_STATUSES:
        return False
    if source["unit"] not in SOURCE_FACT_UNITS[field]:
        return False
    try:
        observation = date.fromisoformat(metadata["observation_date"])
        retrieved = parse_retrieved_at(metadata["retrieved_at"])
    except (TypeError, ValueError):
        return False
    if observation.isoformat() != metadata["observation_date"] or retrieved.date() < observation:
        return False
    identifier = _source_column(source, "instrument_identifier", "source_instrument_identifier")
    identifier_type = _source_column(source, "instrument_identifier_type", "source_instrument_identifier_type")
    if field == "cash_movement":
        if identifier is not None or identifier_type is not None:
            return False
    elif (
        not isinstance(identifier, str) or not identifier.strip()
        or identifier_type not in IDENTIFIER_TYPES
    ):
        return False
    parser_version = source["parser_version"]
    if parser_version.strip().lower() == "unknown":
        return False
    try:
        value = float(source["value"])
    except (TypeError, ValueError):
        return False
    if not math.isfinite(value):
        return False
    if source["field"] in {"price", "benchmark_price", "buy", "sell"}:
        return value > 0
    if source["field"] == "distribution":
        return value >= 0
    return True


def _ensure_instrument(
    connection: sqlite3.Connection,
    *,
    identifier: str,
    identifier_type: str,
    currency: str,
    asset_type: str,
) -> None:
    """Materialize fixture identifiers so explicit links can name both ends."""
    connection.execute(
        """
        INSERT INTO instruments(identifier_type, identifier_value, display_name, asset_type, currency)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(identifier_type, identifier_value) DO NOTHING
        """,
        (identifier_type, identifier, identifier, asset_type, currency),
    )


def add_source_record(
    connection: sqlite3.Connection,
    *,
    source_name: str,
    source_identifier: str,
    retrieved_at: str,
    observation_date: str,
    instrument_identifier: str | None,
    instrument_identifier_type: str | None = None,
    field: str,
    value: str | None,
    unit: str | None,
    currency: str | None,
    freshness_status: str,
    citation_location: str,
    parser_version: str = "unknown",
) -> str:
    """Insert a cited fact and return an identity containing all source metadata."""
    for name, metadata_value in (
        ("source_name", source_name), ("source_identifier", source_identifier),
        ("field", field), ("freshness_status", freshness_status),
        ("citation_location", citation_location), ("parser_version", parser_version),
    ):
        if not isinstance(metadata_value, str) or not metadata_value.strip():
            raise ValueError(f"{name} is required and cannot be blank")
    if freshness_status not in FRESHNESS_STATUSES:
        raise ValueError(f"freshness_status must be one of {sorted(FRESHNESS_STATUSES)}")
    if parser_version.strip().lower() == "unknown":
        raise ValueError("parser_version must identify a real parser version")
    if field not in SUPPORTED_FACT_FIELDS:
        raise ValueError(f"field must be one of {sorted(SUPPORTED_FACT_FIELDS)}")
    if field in REQUIRED_FACT_FIELDS:
        if not isinstance(unit, str) or not unit.strip():
            raise ValueError(f"unit is required for {field} source facts")
        if unit not in SOURCE_FACT_UNITS[field]:
            raise ValueError(
                f"unit {unit!r} is not supported for {field}; "
                f"expected one of {sorted(SOURCE_FACT_UNITS[field])}"
            )
        if not isinstance(currency, str) or not currency.strip():
            raise ValueError(f"currency is required for {field} source facts")
    if field == "cash_movement":
        if instrument_identifier is not None or instrument_identifier_type is not None:
            raise ValueError("cash movement facts cannot have an instrument identity")
    else:
        if not isinstance(instrument_identifier, str) or not instrument_identifier.strip():
            raise ValueError("typed source identifiers are required")
        if instrument_identifier_type not in IDENTIFIER_TYPES:
            raise ValueError("typed source identifiers are required")
    try:
        observation = date.fromisoformat(observation_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("source observation dates must be ISO YYYY-MM-DD") from exc
    if observation.isoformat() != observation_date:
        raise ValueError("source observation dates must be ISO YYYY-MM-DD")
    normalized_retrieved_at = normalize_retrieved_at(retrieved_at)
    retrieved = parse_retrieved_at(normalized_retrieved_at)
    if retrieved.date() < observation:
        raise ValueError("source retrieval date cannot be before observation date")
    if value is None and field in NUMERIC_FIELDS:
        raise ValueError(f"source value for {field} is required")
    if value is not None and field in NUMERIC_FIELDS:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"source value for {field} is not numeric") from exc
        if not math.isfinite(numeric_value):
            raise ValueError(f"source value for {field} must be finite")
        if field in {"price", "benchmark_price", "buy", "sell"} and numeric_value <= 0:
            raise ValueError(f"source value for {field} must be positive")
        if field == "distribution" and numeric_value < 0:
            raise ValueError(f"source value for {field} must be non-negative")
    identity = json.dumps(
        [
            source_name, source_identifier, normalized_retrieved_at, observation_date,
            instrument_identifier_type, instrument_identifier, field, value,
            unit, currency, freshness_status, citation_location, parser_version,
        ],
        ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    )
    source_id = f"SRC-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
    connection.execute(
        """
        INSERT OR IGNORE INTO source_records(
            id, source_name, source_url_or_identifier, retrieved_at, observation_date,
            instrument_identifier, field, value, unit, currency, freshness_status,
            citation_location, instrument_identifier_type, parser_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id, source_name, source_identifier, normalized_retrieved_at, observation_date,
            instrument_identifier, field, value, unit, currency, freshness_status,
            citation_location, instrument_identifier_type, parser_version,
        ),
    )
    return source_id


@dataclass(frozen=True, slots=True)
class PriceFixtureResult:
    accepted_rows: int
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DistributionFixtureResult:
    accepted_rows: int
    source_ids: tuple[str, ...]


def import_price_fixture(
    connection: sqlite3.Connection,
    path: Path,
    *,
    source_name: str,
) -> PriceFixtureResult:
    """Load strict local prices while retaining every explicitly typed observation."""
    required = (
        "identifier", "identifier_type", "observation_date", "price", "currency",
        "retrieved_at", "freshness_status", "citation_location", "benchmark",
    )
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != required:
            raise ValueError("price fixture must use the documented exact columns")
        source_ids: list[str] = []
        for row_number, row in enumerate(reader, 2):
            if row.get(None):
                raise ValueError(f"price fixture row {row_number} has extra fields")
            required_values = (
                "identifier", "identifier_type", "observation_date", "price", "currency",
                "retrieved_at", "freshness_status", "citation_location", "benchmark",
            )
            if any(not row[name].strip() for name in required_values):
                raise ValueError(f"price fixture row {row_number} has a blank required field")
            try:
                price = float(row["price"])
            except ValueError as exc:
                raise ValueError(f"price fixture row {row_number} price is not numeric") from exc
            if not math.isfinite(price):
                raise ValueError(f"price fixture row {row_number} price must be finite")
            if price <= 0:
                raise ValueError(f"price fixture row {row_number} price must be positive")
            identifier_type = row["identifier_type"].strip()
            if identifier_type not in IDENTIFIER_TYPES:
                raise ValueError(f"unsupported identifier type at row {row_number}")
            benchmark = row["benchmark"].strip().lower()
            if benchmark not in {"yes", "no"}:
                raise ValueError(f"benchmark must be yes or no at row {row_number}")
            _ensure_instrument(
                connection, identifier=row["identifier"], identifier_type=identifier_type,
                currency=row["currency"], asset_type="benchmark" if benchmark == "yes" else "security",
            )
            source_id = add_source_record(
                connection, source_name=source_name,
                source_identifier=f"{path.name}#row-{row_number}",
                retrieved_at=row["retrieved_at"], observation_date=row["observation_date"],
                instrument_identifier=row["identifier"],
                instrument_identifier_type=identifier_type,
                field="price" if benchmark == "no" else "benchmark_price",
                value=row["price"], unit="price", currency=row["currency"],
                freshness_status=row["freshness_status"],
                citation_location=row["citation_location"], parser_version=PARSER_VERSION,
            )
            source_ids.append(source_id)
        connection.commit()
    return PriceFixtureResult(len(source_ids), tuple(source_ids))


def import_distribution_fixture(
    connection: sqlite3.Connection,
    path: Path,
    *,
    source_name: str,
) -> DistributionFixtureResult:
    """Load a strict local per-observation distribution fixture.

    Distribution fixtures are deliberately separate from broker cash exports:
    ``distribution_amount`` is an observation for a watchlist/ETF unit, while
    broker ``分配金`` remains a transaction-level ``total_cash`` amount.
    """
    required = (
        "identifier", "identifier_type", "observation_date", "distribution_amount", "unit",
        "currency", "retrieved_at", "freshness_status", "citation_location",
    )
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != required:
            raise ValueError("distribution fixture must use the documented exact columns")
        source_ids: list[str] = []
        for row_number, row in enumerate(reader, 2):
            if row.get(None):
                raise ValueError(f"distribution fixture row {row_number} has extra fields")
            if any(not row[name].strip() for name in required):
                raise ValueError(f"distribution fixture row {row_number} has a blank required field")
            identifier_type = row["identifier_type"].strip()
            if identifier_type not in IDENTIFIER_TYPES:
                raise ValueError(f"unsupported identifier type at row {row_number}")
            try:
                amount = float(row["distribution_amount"])
            except ValueError as exc:
                raise ValueError(f"distribution fixture row {row_number} amount is not numeric") from exc
            if not math.isfinite(amount) or amount < 0:
                raise ValueError(f"distribution fixture row {row_number} amount must be finite and non-negative")
            _ensure_instrument(
                connection, identifier=row["identifier"], identifier_type=identifier_type,
                currency=row["currency"], asset_type="ETF",
            )
            source_ids.append(add_source_record(
                connection,
                source_name=source_name,
                source_identifier=f"{path.name}#row-{row_number}",
                retrieved_at=row["retrieved_at"],
                observation_date=row["observation_date"],
                instrument_identifier=row["identifier"],
                instrument_identifier_type=identifier_type,
                field="distribution",
                value=row["distribution_amount"],
                unit=row["unit"],
                currency=row["currency"],
                freshness_status=row["freshness_status"],
                citation_location=row["citation_location"],
                parser_version=DISTRIBUTION_PARSER_VERSION,
            ))
        connection.commit()
    return DistributionFixtureResult(len(source_ids), tuple(source_ids))
