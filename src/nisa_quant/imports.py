"""Strict importer for the documented synthetic broker CSV format."""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .sources import add_source_record, utc_now


EXPECTED_COLUMNS = (
    "取引日",
    "口座区分",
    "銘柄コード",
    "銘柄名",
    "取引区分",
    "数量",
    "単価",
    "手数料",
    "通貨",
    "分配金",
    "備考",
)
ACCOUNT_ALIASES = {
    "NISA": "NISA",
    "taxable": "taxable",
    "特定口座": "taxable",
    "general": "general",
    "cash": "cash",
    "foreign_currency": "foreign_currency",
}


class CsvImportError(ValueError):
    """Raised when the documented CSV contract is missing or ambiguous."""


@dataclass(frozen=True, slots=True)
class ImportResult:
    accepted_rows: int
    warning_count: int
    import_id: int


def _account_id(connection: sqlite3.Connection, account_type: str) -> int:
    return connection.execute(
        "SELECT id FROM accounts WHERE account_type = ?", (account_type,)
    ).fetchone()[0]


def _instrument(
    connection: sqlite3.Connection, code: str, name: str, currency: str
) -> int:
    identifier_type = "jpx_code" if code.isdigit() else "yahoo_symbol"
    row = connection.execute(
        "SELECT id FROM instruments WHERE identifier_type = ? AND identifier_value = ?",
        (identifier_type, code),
    ).fetchone()
    if row is None:
        instrument_id = connection.execute(
            """
            INSERT INTO instruments(identifier_type, identifier_value, display_name, currency)
            VALUES (?, ?, ?, ?) RETURNING id
            """,
            (identifier_type, code, name, currency),
        ).fetchone()[0]
    else:
        instrument_id = row[0]
    if identifier_type == "jpx_code":
        yahoo_symbol = f"{code}.T"
        yahoo = connection.execute(
            "SELECT id FROM instruments WHERE identifier_type = 'yahoo_symbol' AND identifier_value = ?",
            (yahoo_symbol,),
        ).fetchone()
        if yahoo is None:
            yahoo_id = connection.execute(
                """
                INSERT INTO instruments(identifier_type, identifier_value, display_name, currency)
                VALUES ('yahoo_symbol', ?, ?, ?) RETURNING id
                """,
                (yahoo_symbol, name, currency),
            ).fetchone()[0]
        else:
            yahoo_id = yahoo[0]
        connection.execute(
            "INSERT OR IGNORE INTO instrument_links(from_instrument_id, to_instrument_id, link_type) VALUES (?, ?, 'explicit_yahoo_alias')",
            (instrument_id, yahoo_id),
        )
    return instrument_id


def _float(value: str, *, field: str) -> float:
    try:
        return float(value.replace(",", "")) if value else 0.0
    except ValueError as exc:
        raise ValueError(f"{field} is not numeric") from exc


def import_csv(
    connection: sqlite3.Connection,
    path: Path,
    *,
    source_name: str,
) -> ImportResult:
    """Import a synthetic broker CSV with exact headers and row quarantine."""
    raw = path.read_bytes()
    file_hash = hashlib.sha256(raw).hexdigest()
    existing = connection.execute(
        "SELECT id FROM imports WHERE file_hash = ?", (file_hash,)
    ).fetchone()
    if existing is not None:
        return ImportResult(0, 0, existing[0])
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)):
            raise CsvImportError("ambiguous CSV mapping: duplicate column names")
        if tuple(headers) != EXPECTED_COLUMNS:
            missing = sorted(set(EXPECTED_COLUMNS) - set(headers))
            extra = sorted(set(headers) - set(EXPECTED_COLUMNS))
            raise CsvImportError(f"CSV mapping rejected; missing={missing}, extra={extra}")
        import_id = connection.execute(
            """
            INSERT INTO imports(file_hash, source_name, source_identifier, imported_at)
            VALUES (?, ?, ?, ?) RETURNING id
            """,
            (file_hash, source_name, path.name, utc_now()),
        ).fetchone()[0]
        accepted = 0
        warnings = 0
        for row_number, row in enumerate(reader, 2):
            row_identity = hashlib.sha256(
                "|".join(row.get(column, "") for column in EXPECTED_COLUMNS).encode()
            ).hexdigest()
            try:
                date.fromisoformat(row["取引日"])
                account_type = ACCOUNT_ALIASES.get(row["口座区分"], "unknown_review")
                transaction_type = row["取引区分"]
                quantity = _float(row["数量"], field="数量")
                price = _float(row["単価"], field="単価")
                fee = _float(row["手数料"], field="手数料")
                distribution = _float(row["分配金"], field="分配金")
                currency = row["通貨"]
                if not currency or transaction_type not in {"BUY", "SELL", "DISTRIBUTION", "CASH"}:
                    raise ValueError("unsupported transaction or missing currency")
                if transaction_type == "CASH":
                    account_id = _account_id(connection, account_type)
                    source_id = add_source_record(
                        connection,
                        source_name=source_name,
                        source_identifier=f"{path.name}#row-{row_number}",
                        retrieved_at=utc_now(),
                        observation_date=row["取引日"],
                        instrument_identifier=None,
                        field="cash_movement",
                        value=row["単価"],
                        unit="amount",
                        currency=currency,
                        freshness_status="observed",
                        citation_location=f"{path.name}:row-{row_number}",
                    )
                    if account_type == "unknown_review":
                        raise ValueError("unknown account cannot be posted")
                    connection.execute(
                        "INSERT INTO cash_movements(import_id, account_id, movement_date, amount, currency, source_record_id, row_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (import_id, account_id, row["取引日"], price, currency, source_id, row_identity),
                    )
                else:
                    if not row["銘柄コード"] or (transaction_type != "DISTRIBUTION" and quantity <= 0):
                        raise ValueError("security transaction requires a positive quantity and code")
                    if account_type == "unknown_review":
                        raise ValueError("unknown account cannot be posted")
                    account_id = _account_id(connection, account_type)
                    instrument_id = _instrument(
                        connection, row["銘柄コード"], row["銘柄名"], currency
                    )
                    source_id = add_source_record(
                        connection,
                        source_name=source_name,
                        source_identifier=f"{path.name}#row-{row_number}",
                        retrieved_at=utc_now(),
                        observation_date=row["取引日"],
                        instrument_identifier=row["銘柄コード"],
                        field=transaction_type.lower(),
                        value=row["単価"] or row["分配金"],
                        unit="amount",
                        currency=currency,
                        freshness_status="observed",
                        citation_location=f"{path.name}:row-{row_number}",
                    )
                    connection.execute(
                        """
                        INSERT INTO transactions(
                            import_id, account_id, instrument_id, trade_date, transaction_type,
                            quantity, price, fee, currency, distribution, source_record_id, row_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (import_id, account_id, instrument_id, row["取引日"], transaction_type,
                         quantity, price, fee, currency, distribution, source_id, row_identity),
                    )
                    if transaction_type in {"BUY", "SELL"}:
                        existing_position = connection.execute(
                            "SELECT quantity, cost_basis FROM positions WHERE account_id = ? AND instrument_id = ?",
                            (account_id, instrument_id),
                        ).fetchone()
                        old_quantity = existing_position[0] if existing_position else 0.0
                        old_cost = existing_position[1] if existing_position else 0.0
                        signed_quantity = quantity if transaction_type == "BUY" else -quantity
                        signed_cost = quantity * price + fee if transaction_type == "BUY" else -(quantity * price - fee)
                        connection.execute(
                            """
                            INSERT INTO positions(account_id, instrument_id, quantity, cost_basis)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(account_id, instrument_id) DO UPDATE SET quantity = excluded.quantity, cost_basis = excluded.cost_basis
                            """,
                            (account_id, instrument_id, old_quantity + signed_quantity, old_cost + signed_cost),
                        )
                accepted += 1
            except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
                warnings += 1
                connection.execute(
                    "INSERT INTO data_warnings(import_id, warning_code, message, row_number, created_at) VALUES (?, ?, ?, ?, ?)",
                    (import_id, "UNKNOWN_ACCOUNT" if row.get("口座区分") not in ACCOUNT_ALIASES else "MALFORMED_ROW", str(exc), row_number, utc_now()),
                )
        connection.execute(
            "UPDATE imports SET accepted_rows = ?, warning_count = ? WHERE id = ?",
            (accepted, warnings, import_id),
        )
        connection.commit()
    return ImportResult(accepted, warnings, import_id)
