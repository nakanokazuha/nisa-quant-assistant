"""SQLite schema and connection helpers for the local research ledger."""

from __future__ import annotations

import sqlite3
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
            file_hash TEXT NOT NULL UNIQUE,
            source_name TEXT NOT NULL,
            source_identifier TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            accepted_rows INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0
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
            citation_location TEXT NOT NULL
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
            cost_basis REAL NOT NULL,
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
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            data_cutoff TEXT NOT NULL,
            provider TEXT NOT NULL,
            template_version TEXT NOT NULL,
            source_ids TEXT NOT NULL,
            instrument TEXT NOT NULL,
            label TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            risk TEXT NOT NULL,
            horizon TEXT NOT NULL,
            invalidation TEXT NOT NULL
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
            UNIQUE(recommendation_id, evaluation_date)
        );
        """
    )
    connection.executemany(
        "INSERT OR IGNORE INTO accounts(account_type, display_name) VALUES (?, ?)",
        [(account_type, account_type) for account_type in ACCOUNT_TYPES],
    )
    connection.commit()
