"""Explicit, point-in-time watchlist versions with validated date cutoffs."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone


IDENTIFIER_TYPES = {"jpx_code", "yahoo_symbol", "isin", "other"}


def current_materialization_date() -> str:
    """Return the UTC calendar date used by materialized watchlist tables."""
    return datetime.now(timezone.utc).date().isoformat()


def _effective_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("effective_date must be ISO YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("effective_date must be ISO YYYY-MM-DD")
    return value


def _observed_at(value: str | None) -> str:
    result = value if value is not None else datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return _parse_observed_at(result).isoformat()


def _parse_observed_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("observed_at must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def add_watchlist_item(
    connection: sqlite3.Connection,
    identifier_value: str,
    identifier_type: str,
    display_name: str,
    asset_type: str,
    market: str,
    currency: str,
    benchmark: str | None,
    notes: str,
    *,
    effective_date: str | None = None,
    observed_at: str | None = None,
    benchmark_identifier_type: str | None = None,
    benchmark_identifier_value: str | None = None,
) -> int:
    """Append a version and refresh the current materialized watchlist projection.

    New versions without an explicit effective date become effective on the
    current UTC date.  Historical callers must pass ``effective_date``;
    edits never rewrite an earlier version.  A future-effective version stays
    in the append-only store until the UTC materialization date reaches it.
    """
    if identifier_type not in IDENTIFIER_TYPES:
        raise ValueError("unsupported identifier type")
    if benchmark_identifier_type is not None and benchmark_identifier_type not in IDENTIFIER_TYPES:
        raise ValueError("unsupported benchmark identifier type")
    if benchmark is None and (benchmark_identifier_type is None) != (benchmark_identifier_value is None):
        raise ValueError("benchmark identifier type and value must be supplied together")
    benchmark_value = benchmark_identifier_value or benchmark
    if benchmark_identifier_type is not None and not benchmark_value:
        raise ValueError("benchmark identifier value is required for a typed benchmark")
    if effective_date is None:
        effective_date = datetime.now(timezone.utc).date().isoformat()
    effective = _effective_date(effective_date)
    if observed_at is None:
        today = datetime.now(timezone.utc).date()
        if date.fromisoformat(effective) < today:
            raise ValueError("historical effective_date requires an explicit observed_at")
        observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    observed = _observed_at(observed_at)
    if date.fromisoformat(effective) > datetime.fromisoformat(observed.replace("Z", "+00:00")).date():
        raise ValueError("effective_date cannot be after observed_at")
    version_values = (
        identifier_value, identifier_type, display_name, asset_type, market,
        currency, benchmark, benchmark_identifier_type, benchmark_value,
        notes, effective, observed,
    )
    existing_version = connection.execute(
        """SELECT * FROM watchlist_versions
           WHERE identifier_type = ? AND identifier_value = ? AND effective_from = ?
             AND display_name = ? AND asset_type = ? AND market = ? AND currency = ?
             AND benchmark IS ? AND benchmark_identifier_type IS ?
             AND benchmark_identifier_value IS ? AND notes = ? AND observed_at = ?
           ORDER BY id LIMIT 1""",
        (
            identifier_type, identifier_value, effective, display_name, asset_type, market,
            currency, benchmark, benchmark_identifier_type, benchmark_value, notes, observed,
        ),
    ).fetchone()
    if existing_version is not None:
        version_id = int(existing_version["id"])
    else:
        version_cursor = connection.execute(
            """
            INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type, market,
                currency, benchmark, benchmark_identifier_type,
                benchmark_identifier_value, notes, effective_from, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            version_values,
        )
        version_id = int(version_cursor.fetchone()[0])
    materialize_current(connection, allow_authoritative_update=True)
    connection.commit()
    return version_id


def watchlist_as_of(connection: sqlite3.Connection, *, as_of: str) -> list[sqlite3.Row]:
    """Return each key's latest version effective and observed by ``as_of``."""
    try:
        cutoff = date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError("as_of must be ISO YYYY-MM-DD") from exc
    if cutoff.isoformat() != as_of:
        raise ValueError("as_of must be ISO YYYY-MM-DD")
    versions = connection.execute(
        "SELECT * FROM watchlist_versions",
    ).fetchall()
    selected: dict[tuple[str, str], sqlite3.Row] = {}
    for version in versions:
        try:
            effective = date.fromisoformat(version["effective_from"])
            observed = _parse_observed_at(version["observed_at"])
        except (AttributeError, TypeError, ValueError):
            continue
        if effective.isoformat() != version["effective_from"]:
            continue
        observed_date = observed.date()
        if effective > cutoff or observed_date > cutoff or observed_date < effective:
            continue
        key = (version["identifier_type"], version["identifier_value"])
        if key not in selected or _selection_key(version) > _selection_key(selected[key]):
            selected[key] = version
    for override in connection.execute("SELECT * FROM watchlist_projection_overrides"):
        try:
            effective = date.fromisoformat(override["effective_from"])
            observed = _parse_observed_at(override["observed_at"])
        except (AttributeError, TypeError, ValueError):
            continue
        if effective.isoformat() != override["effective_from"]:
            continue
        observed_date = observed.date()
        if effective > cutoff or observed_date > cutoff or observed_date < effective:
            continue
        key = (override["identifier_type"], override["identifier_value"])
        if key not in selected or _selection_key(override) >= _selection_key(selected[key]):
            selected[key] = override
    return [selected[key] for key in sorted(selected)]


def _selection_key(row: sqlite3.Row) -> tuple[str, datetime, int]:
    """Order versions by effective date, then real observation time, then id."""
    return row["effective_from"], _parse_observed_at(row["observed_at"]), int(row["id"])


def materialize_current(
    connection: sqlite3.Connection,
    *,
    as_of: str | None = None,
    allow_authoritative_update: bool = False,
) -> None:
    """Rebuild current tables from versions available at the UTC cutoff.

    ``watchlist_versions`` is the audit/history store.  ``watchlist`` is a
    projection, so rebuilding it prevents a future-effective edit from
    becoming current merely because it was inserted.  Selected rows are also
    materialized as typed instruments; future-only rows create neither.  A
    default rebuild preserves an existing current row through an explicit
    override when a conflicting version is not an authoritative API replay.
    ``add_watchlist_item`` is the authoritative replay path.
    """
    cutoff = as_of or current_materialization_date()
    selected = watchlist_as_of(connection, as_of=cutoff)
    existing_rows = connection.execute("SELECT * FROM watchlist ORDER BY id").fetchall()
    if not allow_authoritative_update:
        selected_by_key = {
            (row["identifier_type"], row["identifier_value"]): row for row in selected
        }
        watchlist_columns = {row[1] for row in connection.execute("PRAGMA table_info(watchlist)")}
        for current in existing_rows:
            key = (current["identifier_type"], current["identifier_value"])
            selected_row = selected_by_key.get(key)
            if selected_row is None or _watchlist_metadata(current) == _watchlist_metadata(selected_row):
                continue
            if connection.execute(
                """
                SELECT 1 FROM watchlist_versions
                WHERE identifier_type = ? AND identifier_value = ?
                  AND display_name = ? AND asset_type = ? AND market = ?
                  AND currency = ? AND benchmark IS ?
                  AND benchmark_identifier_type IS ?
                  AND benchmark_identifier_value IS ? AND notes = ?
                LIMIT 1
                """,
                (
                    current["identifier_type"], current["identifier_value"],
                    current["display_name"], current["asset_type"], current["market"],
                    current["currency"], current["benchmark"],
                    current["benchmark_identifier_type"],
                    current["benchmark_identifier_value"], current["notes"],
                ),
            ).fetchone() is not None:
                continue
            # A legacy current row is only a safe override at the projection
            # cutoff that is rebuilding it.  Reusing the selected historical
            # version's effective date would make current/future metadata
            # visible in earlier point-in-time queries.
            if as_of is not None and selected_row["effective_from"] != cutoff:
                continue
            effective = cutoff
            observed = _current_observed_at(current, watchlist_columns, effective)
            connection.execute(
                """
                INSERT INTO watchlist_projection_overrides(
                    identifier_value, identifier_type, display_name, asset_type,
                    market, currency, benchmark, benchmark_identifier_type,
                    benchmark_identifier_value, notes, effective_from, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identifier_type, identifier_value, effective_from) DO UPDATE SET
                    display_name = excluded.display_name,
                    asset_type = excluded.asset_type,
                    market = excluded.market,
                    currency = excluded.currency,
                    benchmark = excluded.benchmark,
                    benchmark_identifier_type = excluded.benchmark_identifier_type,
                    benchmark_identifier_value = excluded.benchmark_identifier_value,
                    notes = excluded.notes,
                    observed_at = excluded.observed_at
                """,
                (
                    current["identifier_value"], current["identifier_type"],
                    current["display_name"], current["asset_type"], current["market"],
                    current["currency"], current["benchmark"],
                    current["benchmark_identifier_type"],
                    current["benchmark_identifier_value"], current["notes"],
                    effective,
                    observed,
                ),
            )
        selected = watchlist_as_of(connection, as_of=cutoff)
    prior_order = {
        (row["identifier_type"], row["identifier_value"]): index
        for index, row in enumerate(existing_rows)
    }
    selected.sort(
        key=lambda row: prior_order.get(
            (row["identifier_type"], row["identifier_value"]), len(prior_order),
        )
    )
    connection.execute("DELETE FROM watchlist")
    watchlist_columns = {row[1] for row in connection.execute("PRAGMA table_info(watchlist)")}
    for row in selected:
        materialized_columns = [
            "identifier_value", "identifier_type", "display_name", "asset_type",
            "market", "currency", "benchmark", "benchmark_identifier_type",
            "benchmark_identifier_value", "notes",
        ]
        materialized_values: list[object] = [
            row["identifier_value"], row["identifier_type"], row["display_name"],
            row["asset_type"], row["market"], row["currency"], row["benchmark"],
            row["benchmark_identifier_type"], row["benchmark_identifier_value"],
            row["notes"],
        ]
        for column, value in (
            ("effective_date", row["effective_from"]),
            ("effective_from", row["effective_from"]),
            ("observed_at", row["observed_at"]),
        ):
            if column in watchlist_columns:
                materialized_columns.append(column)
                materialized_values.append(value)
        placeholders = ", ".join("?" for _ in materialized_columns)
        connection.execute(
            f"INSERT INTO watchlist({', '.join(materialized_columns)}) VALUES ({placeholders})",
            materialized_values,
        )
        connection.execute(
            """
            INSERT INTO instruments(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, benchmark, benchmark_identifier_type,
                benchmark_identifier_value, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                display_name = excluded.display_name,
                asset_type = excluded.asset_type,
                market = excluded.market,
                currency = excluded.currency,
                benchmark = excluded.benchmark,
                benchmark_identifier_type = excluded.benchmark_identifier_type,
                benchmark_identifier_value = excluded.benchmark_identifier_value,
                notes = excluded.notes
            """,
            (
                row["identifier_value"], row["identifier_type"], row["display_name"],
                row["asset_type"], row["market"], row["currency"], row["benchmark"],
                row["benchmark_identifier_type"], row["benchmark_identifier_value"],
                row["notes"],
            ),
        )


def _watchlist_metadata(row: sqlite3.Row) -> tuple[object, ...]:
    return tuple(
        row[key]
        for key in (
            "identifier_value", "identifier_type", "display_name", "asset_type",
            "market", "currency", "benchmark", "benchmark_identifier_type",
            "benchmark_identifier_value", "notes",
        )
    )


def _current_observed_at(
    row: sqlite3.Row,
    columns: set[str],
    effective: str,
) -> str:
    if "observed_at" in columns:
        value = row["observed_at"]
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            normalized = parsed.astimezone(timezone.utc)
            effective_date = date.fromisoformat(effective)
            if normalized.date() < effective_date:
                normalized = datetime.combine(
                    effective_date, datetime.min.time(), tzinfo=timezone.utc,
                )
            return normalized.isoformat()
    return f"{effective}T00:00:00+00:00"
