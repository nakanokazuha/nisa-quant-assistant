"""SQLite schema and connection helpers for the local research ledger.

All records are local, source-backed, and audit-oriented.  Watchlist metadata
is append-only in ``watchlist_versions``; the ``watchlist`` and ``instruments``
tables are current-UTC projections, so a cutoff cannot see later edits.
Legacy watchlist migration is resumable and idempotent.  The schema has no
broker or order execution surface.  When a legacy current row collides with
an incomplete same-day version, its safe projection is retained separately
from the append-only version history.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path


ACCOUNT_TYPES = (
    "NISA",
    "taxable",
    "general",
    "cash",
    "foreign_currency",
    "unknown_review",
)


def connect_database(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite database with foreign keys and dictionary-like rows."""
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _legacy_watchlist_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _legacy_watchlist_date(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == value else None


def _migrate_watchlist_rows(connection: sqlite3.Connection) -> None:
    """Resume legacy-row migration without sentinel dates or duplicates."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(watchlist)")}
    rows = connection.execute("SELECT * FROM watchlist").fetchall()
    seeded_now = datetime.now(timezone.utc).replace(microsecond=0)
    seeded_date = seeded_now.date().isoformat()
    for row in rows:
        effective = None
        for column in ("effective_date", "effective_from"):
            if column in columns:
                effective = _legacy_watchlist_date(row[column])
                if effective is not None:
                    break
        observed = _legacy_watchlist_timestamp(row["observed_at"]) if "observed_at" in columns else None
        if effective is None:
            effective = observed[:10] if observed is not None else seeded_date
        if observed is None:
            # No observed field means the legacy row has no historical
            # observation.  Seed the current UTC instant, or the future
            # effective date at midnight when necessary to keep the pair
            # internally valid; future metadata remains hidden by projection.
            observed = max(seeded_now, datetime.fromisoformat(f"{effective}T00:00:00+00:00")).isoformat()
        key_without_date = (row["identifier_type"], row["identifier_value"])
        try:
            legacy_effective = date.fromisoformat(effective)
            legacy_observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        except ValueError:
            legacy_effective = date.fromisoformat(seeded_date)
            legacy_observed = seeded_now
        if legacy_observed.tzinfo is None:
            legacy_observed = legacy_observed.replace(tzinfo=timezone.utc)
        if legacy_observed.date() < legacy_effective:
            legacy_observed = datetime.combine(
                legacy_effective, datetime.min.time(), tzinfo=timezone.utc,
            )
        effective = legacy_effective.isoformat()
        observed = legacy_observed.astimezone(timezone.utc).isoformat()
        legacy_values = (
            row["identifier_value"], row["identifier_type"], row["display_name"],
            row["asset_type"], row["market"], row["currency"], row["benchmark"],
            row["benchmark_identifier_type"], row["benchmark_identifier_value"],
            row["notes"], effective, observed,
        )
        current_version_exists = False
        conflicting_versions: list[sqlite3.Row] = []
        for version in connection.execute(
            "SELECT * FROM watchlist_versions "
            "WHERE identifier_type = ? AND identifier_value = ?",
            key_without_date,
        ):
            version_values = tuple(version[key] for key in (
                "identifier_value", "identifier_type", "display_name", "asset_type",
                "market", "currency", "benchmark", "benchmark_identifier_type",
                "benchmark_identifier_value", "notes", "effective_from", "observed_at",
            ))
            metadata_matches = version_values[:10] == legacy_values[:10]
            try:
                version_effective = date.fromisoformat(version["effective_from"])
            except (AttributeError, TypeError, ValueError):
                version_effective = None
            if (
                version_effective is not None
                and version_effective <= seeded_now.date()
                and version_effective >= legacy_effective
                and version_values[:10] != legacy_values[:10]
            ):
                conflicting_versions.append(version)
            version_is_current = False
            try:
                version_observed = datetime.fromisoformat(version["observed_at"].replace("Z", "+00:00"))
                version_is_current = (
                    version_effective is not None
                    and
                    version_effective <= seeded_now.date()
                    and version_observed.tzinfo is not None
                    and version_observed.astimezone(timezone.utc).date() <= seeded_now.date()
                    and version_observed.astimezone(timezone.utc).date() >= version_effective
                )
            except (AttributeError, TypeError, ValueError):
                pass
            if version_values == legacy_values or (metadata_matches and version_is_current):
                current_version_exists = True
        for version in conflicting_versions:
            try:
                conflict_effective = date.fromisoformat(version["effective_from"])
                conflict_observed = datetime.fromisoformat(version["observed_at"].replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError):
                continue
            if conflict_observed.tzinfo is None:
                conflict_observed = conflict_observed.replace(tzinfo=timezone.utc)
            conflict_observed = conflict_observed.astimezone(timezone.utc)
            if conflict_observed.date() < conflict_effective:
                conflict_observed = datetime.combine(
                    conflict_effective, datetime.min.time(), tzinfo=timezone.utc,
                )
            override_observed = max(
                legacy_observed,
                datetime.combine(conflict_effective, datetime.min.time(), tzinfo=timezone.utc),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO watchlist_projection_overrides(
                    identifier_value, identifier_type, display_name, asset_type,
                    market, currency, benchmark, benchmark_identifier_type,
                    benchmark_identifier_value, notes, effective_from, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                # Preserve the legacy row's observation timestamp.  Reusing
                # the conflicting version's timestamp could make late legacy
                # metadata appear in an earlier point-in-time projection.
                (*legacy_values[:10], conflict_effective.isoformat(), override_observed.isoformat()),
            )
        # An older, future-only, or partial version is not evidence that this
        # current projection was migrated.  Only an exact replay is resumable.
        if current_version_exists:
            continue
        key = (*key_without_date, effective)
        exists = connection.execute(
            "SELECT 1 FROM watchlist_versions WHERE identifier_type = ? AND identifier_value = ? AND effective_from = ?",
            key,
        ).fetchone()
        if exists is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO watchlist_projection_overrides(
                    identifier_value, identifier_type, display_name, asset_type,
                    market, currency, benchmark, benchmark_identifier_type,
                    benchmark_identifier_value, notes, effective_from, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                legacy_values,
            )
            continue
        connection.execute(
            """
            INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type, market,
                currency, benchmark, benchmark_identifier_type,
                benchmark_identifier_value, notes, effective_from, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["identifier_value"], row["identifier_type"], row["display_name"],
                row["asset_type"], row["market"], row["currency"], row["benchmark"],
                row["benchmark_identifier_type"], row["benchmark_identifier_value"],
                row["notes"], effective, observed,
            ),
        )


def _materialize_watchlist(connection: sqlite3.Connection) -> None:
    from .watchlist import materialize_current

    materialize_current(connection)


def _migrate_watchlist_versions_for_corrections(connection: sqlite3.Connection) -> None:
    """Remove the legacy one-version-per-effective-date uniqueness constraint."""
    for index in connection.execute("PRAGMA index_list(watchlist_versions)").fetchall():
        if not index[2]:
            continue
        columns = [row[2] for row in connection.execute(f"PRAGMA index_info({index[1]!r})").fetchall()]
        if columns != ["identifier_type", "identifier_value", "effective_from"]:
            continue
        connection.execute("ALTER TABLE watchlist_versions RENAME TO watchlist_versions_legacy")
        connection.execute(
            """
            CREATE TABLE watchlist_versions (
                id INTEGER PRIMARY KEY,
                identifier_value TEXT NOT NULL,
                identifier_type TEXT NOT NULL CHECK (identifier_type IN ('jpx_code', 'yahoo_symbol', 'isin', 'other')),
                display_name TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                market TEXT NOT NULL,
                currency TEXT NOT NULL,
                benchmark TEXT,
                benchmark_identifier_type TEXT,
                benchmark_identifier_value TEXT,
                notes TEXT NOT NULL DEFAULT '',
                effective_from TEXT NOT NULL,
                observed_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """INSERT INTO watchlist_versions(
                id, identifier_value, identifier_type, display_name, asset_type, market,
                currency, benchmark, benchmark_identifier_type, benchmark_identifier_value,
                notes, effective_from, observed_at
            ) SELECT id, identifier_value, identifier_type, display_name, asset_type, market,
                currency, benchmark, benchmark_identifier_type, benchmark_identifier_value,
                notes, effective_from, observed_at FROM watchlist_versions_legacy"""
        )
        connection.execute("DROP TABLE watchlist_versions_legacy")
        break


def _legacy_cost_basis(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> float | None:
    """Keep a legacy scalar only when its currency context is unambiguous."""
    try:
        basis = float(row["cost_basis"])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(basis) or basis < 0:
        return None
    instrument = connection.execute(
        "SELECT currency FROM instruments WHERE id = ?", (row["instrument_id"],)
    ).fetchone()
    if instrument is None or not instrument[0]:
        return None
    currencies = {
        transaction[0]
        for transaction in connection.execute(
            "SELECT DISTINCT currency FROM transactions WHERE account_id = ? AND instrument_id = ?",
            (row["account_id"], row["instrument_id"]),
        )
        if transaction[0]
    }
    if len(currencies) > 1 or (currencies and currencies != {instrument[0]}):
        return None
    return basis


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the ledger schema and the fixed account-type rows."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS instruments (
            id INTEGER PRIMARY KEY,
            identifier_type TEXT NOT NULL CHECK (identifier_type IN ('jpx_code', 'yahoo_symbol', 'isin', 'other')),
            identifier_value TEXT NOT NULL,
            display_name TEXT NOT NULL,
            asset_type TEXT NOT NULL DEFAULT 'security',
            currency TEXT NOT NULL DEFAULT 'JPY',
            market TEXT,
            benchmark TEXT,
            benchmark_identifier_type TEXT,
            benchmark_identifier_value TEXT,
            notes TEXT,
            UNIQUE(identifier_type, identifier_value)
        );
        CREATE TABLE IF NOT EXISTS instrument_links (
            from_instrument_id INTEGER NOT NULL REFERENCES instruments(id),
            to_instrument_id INTEGER NOT NULL REFERENCES instruments(id),
            link_type TEXT NOT NULL,
            PRIMARY KEY(from_instrument_id, to_instrument_id, link_type)
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY,
            account_type TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY,
            file_hash TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_identifier TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            accepted_rows INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(file_hash, source_name, source_identifier)
        );
        CREATE TABLE IF NOT EXISTS source_records (
            id TEXT PRIMARY KEY,
            source_name TEXT NOT NULL,
            source_url_or_identifier TEXT NOT NULL,
            retrieved_at TEXT NOT NULL,
            observation_date TEXT,
            instrument_identifier TEXT,
            field TEXT NOT NULL,
            value TEXT,
            unit TEXT,
            currency TEXT,
            freshness_status TEXT NOT NULL,
            citation_location TEXT NOT NULL,
            instrument_identifier_type TEXT,
            parser_version TEXT NOT NULL DEFAULT 'unknown'
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY,
            import_id INTEGER NOT NULL REFERENCES imports(id),
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            instrument_id INTEGER REFERENCES instruments(id),
            trade_date TEXT NOT NULL,
            transaction_type TEXT NOT NULL CHECK (transaction_type IN ('BUY', 'SELL', 'DISTRIBUTION')),
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            fee REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL,
            distribution REAL NOT NULL DEFAULT 0,
            source_record_id TEXT NOT NULL REFERENCES source_records(id),
            row_hash TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS cash_movements (
            id INTEGER PRIMARY KEY,
            import_id INTEGER NOT NULL REFERENCES imports(id),
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            movement_date TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            source_record_id TEXT NOT NULL REFERENCES source_records(id),
            row_hash TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS positions (
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            instrument_id INTEGER NOT NULL REFERENCES instruments(id),
            quantity REAL NOT NULL,
            cost_basis REAL,
            latest_price REAL,
            price_date TEXT,
            price_status TEXT NOT NULL DEFAULT 'unavailable',
            PRIMARY KEY(account_id, instrument_id)
        );
        CREATE TABLE IF NOT EXISTS data_warnings (
            id INTEGER PRIMARY KEY,
            import_id INTEGER REFERENCES imports(id),
            warning_code TEXT NOT NULL,
            message TEXT NOT NULL,
            row_number INTEGER,
            severity TEXT NOT NULL DEFAULT 'warning',
            created_at TEXT NOT NULL,
            observation_date TEXT
        );
        CREATE TABLE IF NOT EXISTS review_quarantine (
            id INTEGER PRIMARY KEY,
            import_id INTEGER NOT NULL REFERENCES imports(id),
            row_number INTEGER NOT NULL,
            reason TEXT NOT NULL,
            row_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY,
            identifier_value TEXT NOT NULL,
            identifier_type TEXT NOT NULL CHECK (identifier_type IN ('jpx_code', 'yahoo_symbol', 'isin', 'other')),
            display_name TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            market TEXT NOT NULL,
            currency TEXT NOT NULL,
            benchmark TEXT,
            benchmark_identifier_type TEXT,
            benchmark_identifier_value TEXT,
            notes TEXT NOT NULL DEFAULT '',
            UNIQUE(identifier_type, identifier_value)
        );
        CREATE TABLE IF NOT EXISTS watchlist_versions (
            id INTEGER PRIMARY KEY,
            identifier_value TEXT NOT NULL,
            identifier_type TEXT NOT NULL CHECK (identifier_type IN ('jpx_code', 'yahoo_symbol', 'isin', 'other')),
            display_name TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            market TEXT NOT NULL,
            currency TEXT NOT NULL,
            benchmark TEXT,
            benchmark_identifier_type TEXT,
            benchmark_identifier_value TEXT,
            notes TEXT NOT NULL DEFAULT '',
            effective_from TEXT NOT NULL,
            observed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watchlist_projection_overrides (
            id INTEGER PRIMARY KEY,
            identifier_value TEXT NOT NULL,
            identifier_type TEXT NOT NULL,
            display_name TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            market TEXT NOT NULL,
            currency TEXT NOT NULL,
            benchmark TEXT,
            benchmark_identifier_type TEXT,
            benchmark_identifier_value TEXT,
            notes TEXT NOT NULL DEFAULT '',
            effective_from TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            UNIQUE(identifier_type, identifier_value, effective_from)
        );
        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            data_cutoff TEXT NOT NULL,
            provider TEXT NOT NULL,
            template_version TEXT NOT NULL,
            provider_contract TEXT NOT NULL DEFAULT '',
            template_contract TEXT NOT NULL DEFAULT '',
            source_ids TEXT NOT NULL,
            instrument TEXT NOT NULL,
            label TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            risk TEXT NOT NULL,
            horizon TEXT NOT NULL,
            invalidation TEXT NOT NULL,
            snapshot_json TEXT NOT NULL DEFAULT '{}',
            snapshot_hash TEXT NOT NULL DEFAULT '',
            contract_hash TEXT NOT NULL DEFAULT '',
            identifier_type TEXT,
            identifier_value TEXT,
            benchmark_identifier_type TEXT,
            benchmark_identifier_value TEXT,
            currency TEXT,
            freshness_status TEXT
        );
        CREATE TABLE IF NOT EXISTS recommendation_outcomes (
            id INTEGER PRIMARY KEY,
            recommendation_id INTEGER NOT NULL REFERENCES recommendations(id),
            evaluation_date TEXT NOT NULL,
            observed_price REAL,
            benchmark_price REAL,
            observed_return REAL,
            benchmark_return REAL,
            snapshot_json TEXT NOT NULL,
            observed_price_source_id TEXT,
            benchmark_price_source_id TEXT,
            UNIQUE(recommendation_id, evaluation_date)
        );
        CREATE TABLE IF NOT EXISTS recommendation_integrity (
            recommendation_id INTEGER PRIMARY KEY REFERENCES recommendations(id),
            provider TEXT NOT NULL,
            provider_contract TEXT NOT NULL,
            template_version TEXT NOT NULL,
            template_contract TEXT NOT NULL,
            contract_hash TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS recommendation_integrity_no_update
        BEFORE UPDATE ON recommendation_integrity
        BEGIN
            SELECT RAISE(ABORT, 'recommendation integrity evidence is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS recommendation_integrity_no_delete
        BEFORE DELETE ON recommendation_integrity
        BEGIN
            SELECT RAISE(ABORT, 'recommendation integrity evidence is immutable');
        END;
        """
    )
    positions_columns = list(connection.execute("PRAGMA table_info(positions)"))
    cost_basis_column = next(
        (row for row in positions_columns if row[1] == "cost_basis"), None
    )
    if cost_basis_column is not None and cost_basis_column[3]:
        # SQLite cannot drop NOT NULL in place.  Rebuild this small derived
        # table so mixed-currency positions can carry an explicit unavailable
        # scalar cost basis instead of a fabricated number.
        legacy_rows = connection.execute("SELECT * FROM positions").fetchall()
        connection.execute("ALTER TABLE positions RENAME TO positions_legacy")
        connection.execute(
            """
            CREATE TABLE positions (
                account_id INTEGER NOT NULL REFERENCES accounts(id),
                instrument_id INTEGER NOT NULL REFERENCES instruments(id),
                quantity REAL NOT NULL,
                cost_basis REAL,
                latest_price REAL,
                price_date TEXT,
                price_status TEXT NOT NULL DEFAULT 'unavailable',
                PRIMARY KEY(account_id, instrument_id)
            )
            """
        )
        for row in legacy_rows:
            connection.execute(
                """
                INSERT INTO positions(
                    account_id, instrument_id, quantity, cost_basis,
                    latest_price, price_date, price_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["account_id"], row["instrument_id"], row["quantity"],
                    _legacy_cost_basis(connection, row), row["latest_price"],
                    row["price_date"], row["price_status"],
                ),
            )
        connection.execute("DROP TABLE positions_legacy")
    # Keep initialization safe for a database created by the first slice.
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(instruments)")
    }
    for name, definition in (
        ("market", "TEXT"),
        ("benchmark", "TEXT"),
        ("benchmark_identifier_type", "TEXT"),
        ("benchmark_identifier_value", "TEXT"),
        ("notes", "TEXT"),
    ):
        if name not in existing_columns:
            connection.execute(f"ALTER TABLE instruments ADD COLUMN {name} {definition}")
    source_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(source_records)")
    }
    if "instrument_identifier_type" not in source_columns:
        connection.execute("ALTER TABLE source_records ADD COLUMN instrument_identifier_type TEXT")
    if "parser_version" not in source_columns:
        connection.execute("ALTER TABLE source_records ADD COLUMN parser_version TEXT NOT NULL DEFAULT 'unknown'")
    warning_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(data_warnings)")
    }
    if "observation_date" not in warning_columns:
        connection.execute("ALTER TABLE data_warnings ADD COLUMN observation_date TEXT")
    watchlist_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(watchlist)")
    }
    for name, definition in (
        ("benchmark_identifier_type", "TEXT"),
        ("benchmark_identifier_value", "TEXT"),
    ):
        if name not in watchlist_columns:
            connection.execute(f"ALTER TABLE watchlist ADD COLUMN {name} {definition}")
    recommendation_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(recommendations)")
    }
    for name, definition in (
        ("snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("snapshot_hash", "TEXT NOT NULL DEFAULT ''"),
        ("identifier_type", "TEXT"),
        ("identifier_value", "TEXT"),
    ):
        if name not in recommendation_columns:
            connection.execute(f"ALTER TABLE recommendations ADD COLUMN {name} {definition}")
    outcome_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(recommendation_outcomes)")
    }
    for name in ("observed_price_source_id", "benchmark_price_source_id"):
        if name not in outcome_columns:
            connection.execute(f"ALTER TABLE recommendation_outcomes ADD COLUMN {name} TEXT")
    for name, definition in (
        ("provider_contract", "TEXT NOT NULL DEFAULT ''"),
        ("template_contract", "TEXT NOT NULL DEFAULT ''"),
        ("benchmark_identifier_type", "TEXT"),
        ("benchmark_identifier_value", "TEXT"),
        ("currency", "TEXT"),
        ("freshness_status", "TEXT"),
        ("contract_hash", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in recommendation_columns:
            connection.execute(f"ALTER TABLE recommendations ADD COLUMN {name} {definition}")
    connection.executemany(
        "INSERT OR IGNORE INTO accounts(account_type, display_name) VALUES (?, ?)",
        [(account_type, account_type) for account_type in ACCOUNT_TYPES],
    )
    # Migrate on every initialization: a previous interrupted migration can
    # leave an empty or partial version table beside legacy current rows.
    # ``watchlist_versions`` remains the history store; materialized tables are
    # rebuilt from the current UTC projection below.
    _migrate_watchlist_versions_for_corrections(connection)
    _migrate_watchlist_rows(connection)
    _materialize_watchlist(connection)
    connection.commit()


def link_instruments(
    connection: sqlite3.Connection,
    *,
    from_identifier_type: str,
    from_identifier_value: str,
    to_identifier_type: str,
    to_identifier_value: str,
    link_type: str,
) -> None:
    """Persist an explicitly supplied typed alias/link between known instruments.

    This API never creates instruments and never normalizes one identifier into
    another.  A missing endpoint therefore fails closed instead of becoming an
    inferred ``.T`` alias.
    """
    allowed_types = {"jpx_code", "yahoo_symbol", "isin", "other"}
    if from_identifier_type not in allowed_types or to_identifier_type not in allowed_types:
        raise ValueError("instrument link endpoints require supported identifier types")
    if not link_type.strip():
        raise ValueError("instrument link type is required")
    from_row = connection.execute(
        "SELECT id FROM instruments WHERE identifier_type = ? AND identifier_value = ?",
        (from_identifier_type, from_identifier_value),
    ).fetchone()
    to_row = connection.execute(
        "SELECT id FROM instruments WHERE identifier_type = ? AND identifier_value = ?",
        (to_identifier_type, to_identifier_value),
    ).fetchone()
    if from_row is None or to_row is None:
        raise ValueError("instrument link endpoints must already exist")
    if from_row[0] == to_row[0]:
        raise ValueError("instrument link endpoints must be distinct")
    connection.execute(
        "INSERT OR IGNORE INTO instrument_links(from_instrument_id, to_instrument_id, link_type) VALUES (?, ?, ?)",
        (from_row[0], to_row[0], link_type),
    )
    connection.commit()


def add_instrument_link(
    connection: sqlite3.Connection,
    *,
    from_identifier_type: str,
    from_identifier_value: str,
    to_identifier_type: str,
    to_identifier_value: str,
    link_type: str,
) -> None:
    """Backward-compatible name for the explicit typed link API."""
    link_instruments(
        connection,
        from_identifier_type=from_identifier_type,
        from_identifier_value=from_identifier_value,
        to_identifier_type=to_identifier_type,
        to_identifier_value=to_identifier_value,
        link_type=link_type,
    )
