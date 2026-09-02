"""Strict importer for the explicit, synthetic broker CSV contract.

Rows that cannot be safely normalized are retained in ``review_quarantine``
and represented by a warning.  No identifier or account aliases are guessed.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .sources import add_source_record, normalize_retrieved_at, safe_add, safe_divide, safe_multiply, utc_now


EXPECTED_COLUMNS = (
    "取引日", "口座区分", "銘柄コード", "銘柄コード種別", "銘柄名", "取引区分",
    "数量", "単価", "手数料", "通貨", "分配金", "備考",
)
ACCOUNT_ALIASES = {
    "NISA": "NISA",
    "taxable": "taxable",
    "特定口座": "taxable",
    "general": "general",
    "cash": "cash",
    "foreign_currency": "foreign_currency",
}
IDENTIFIER_TYPES = {"jpx_code", "yahoo_symbol", "isin", "other"}
PARSER_VERSION = "broker-csv-v2"


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
    connection: sqlite3.Connection,
    code: str,
    identifier_type: str,
    name: str,
    currency: str,
) -> int:
    if identifier_type not in IDENTIFIER_TYPES:
        raise ValueError("identifier type is missing or unsupported")
    row = connection.execute(
        "SELECT id FROM instruments WHERE identifier_type = ? AND identifier_value = ?",
        (identifier_type, code),
    ).fetchone()
    if row is not None:
        return int(row[0])
    return int(connection.execute(
        """
        INSERT INTO instruments(identifier_type, identifier_value, display_name, currency)
        VALUES (?, ?, ?, ?) RETURNING id
        """,
        (identifier_type, code, name, currency),
    ).fetchone()[0])


def _float(value: str, *, field: str, required: bool = True) -> float:
    if not value or not value.strip():
        if required:
            raise ValueError(f"{field} is required and cannot be blank")
        return 0.0
    try:
        parsed = float(value.replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"{field} is not numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


def _security_values_are_valid(
    transaction_type: str,
    price: float,
    fee: float,
    distribution: float,
) -> None:
    if fee < 0:
        raise ValueError("手数料 must be non-negative")
    if distribution < 0:
        raise ValueError("分配金 must be non-negative")
    if transaction_type in {"BUY", "SELL"} and price <= 0:
        raise ValueError("単価 must be positive for BUY/SELL")


def _transaction_values_are_valid(row: sqlite3.Row) -> bool:
    try:
        quantity = float(row["quantity"])
        price = float(row["price"])
        fee = float(row["fee"])
        distribution = float(row["distribution"])
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(value) for value in (quantity, price, fee, distribution)):
        return False
    if fee < 0 or distribution < 0:
        return False
    return row["transaction_type"] not in {"BUY", "SELL"} or (
        quantity > 0 and price > 0
    )


def _valid_date(value: str | None) -> bool:
    if not value:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _quarantine(
    connection: sqlite3.Connection,
    *,
    import_id: int,
    row_number: int,
    row: dict[str, str | list[str] | None],
    reason: str,
    warning_code: str,
    created_at: str,
) -> None:
    observation_date = row.get("取引日")
    if not isinstance(observation_date, str) or not _valid_date(observation_date):
        observation_date = None
    connection.execute(
        "INSERT INTO review_quarantine(import_id, row_number, reason, row_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (import_id, row_number, reason, _row_json(row), created_at),
    )
    connection.execute(
        "INSERT INTO data_warnings(import_id, warning_code, message, row_number, created_at, observation_date) VALUES (?, ?, ?, ?, ?, ?)",
        (import_id, warning_code, reason, row_number, created_at, observation_date),
    )


def _row_json(row: dict[str, str | list[str] | None]) -> str:
    """Serialize DictReader rows, including overflow under a stable key."""
    normalized = {("__extra__" if key is None else key): value for key, value in row.items()}
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _rebuild_positions(connection: sqlite3.Connection) -> list[tuple[sqlite3.Row, str, str]]:
    """Recalculate positions without scalarizing mixed-currency cost basis."""
    state: dict[tuple[int, int], dict[str, float]] = {}
    currency_state: dict[tuple[int, int, str], dict[str, float]] = {}
    position_currencies: dict[tuple[int, int], set[str]] = {}
    invalid: list[tuple[sqlite3.Row, str, str]] = []
    rows = connection.execute(
        """
        SELECT t.*, a.account_type
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.account_type IN ('NISA', 'taxable', 'general')
        ORDER BY t.trade_date, t.id
        """
    ).fetchall()
    for row in rows:
        if not _transaction_values_are_valid(row):
            invalid.append((row, "BUY/SELL price and fee values must be finite, positive, and non-negative respectively", "INVALID_TRANSACTION_VALUES"))
            continue
        if row["transaction_type"] == "DISTRIBUTION":
            continue
        key = (row["account_id"], row["instrument_id"])
        position = state.setdefault(key, {"quantity": 0.0, "cost_basis": 0.0})
        position_currencies.setdefault(key, set()).add(row["currency"])
        currency_key = (*key, row["currency"])
        currency_position = currency_state.setdefault(
            currency_key, {"quantity": 0.0, "cost_basis": 0.0},
        )
        if row["transaction_type"] == "BUY":
            transaction_cost = safe_add(
                safe_multiply(row["quantity"], row["price"]) or math.inf,
                row["fee"],
            )
            next_values = (
                safe_add(position["quantity"], row["quantity"]),
                safe_add(position["cost_basis"], transaction_cost or math.inf),
                safe_add(currency_position["quantity"], row["quantity"]),
                safe_add(currency_position["cost_basis"], transaction_cost or math.inf),
            )
            if transaction_cost is None or None in next_values:
                invalid.append((row, "transaction arithmetic overflowed; values are unavailable", "NONFINITE_DERIVED_VALUE"))
                continue
            position["quantity"], position["cost_basis"] = next_values[:2]
            currency_position["quantity"], currency_position["cost_basis"] = next_values[2:]
            continue
        if row["quantity"] > position["quantity"]:
            invalid.append((row, "sell quantity exceeds available quantity after chronological normalization", "MALFORMED_ROW"))
            continue
        if row["quantity"] > currency_position["quantity"]:
            invalid.append((row, "sell currency has no reconciled same-currency lot and no cited FX policy", "MIXED_CURRENCY_NO_FX"))
            continue
        average_basis = safe_divide(currency_position["cost_basis"], currency_position["quantity"])
        sold_basis = safe_multiply(row["quantity"], average_basis) if average_basis is not None else None
        next_values = (
            safe_add(position["quantity"], -row["quantity"]),
            safe_add(position["cost_basis"], -(sold_basis or math.inf)),
            safe_add(currency_position["quantity"], -row["quantity"]),
            safe_add(currency_position["cost_basis"], -(sold_basis or math.inf)),
        )
        if sold_basis is None or sold_basis > currency_position["cost_basis"] or None in next_values:
            invalid.append((row, "transaction arithmetic overflowed; values are unavailable", "NONFINITE_DERIVED_VALUE"))
            continue
        position["quantity"], position["cost_basis"] = next_values[:2]
        currency_position["quantity"], currency_position["cost_basis"] = next_values[2:]
    connection.execute("DELETE FROM positions")
    for (account_id, instrument_id), position in state.items():
        cost_basis = (
            position["cost_basis"]
            if len(position_currencies[(account_id, instrument_id)]) == 1
            else None
        )
        connection.execute(
            "INSERT INTO positions(account_id, instrument_id, quantity, cost_basis) VALUES (?, ?, ?, ?)",
            (account_id, instrument_id, position["quantity"], cost_basis),
        )
    return invalid


def import_csv(
    connection: sqlite3.Connection,
    path: Path,
    *,
    source_name: str,
    retrieved_at: str | None = None,
) -> ImportResult:
    """Import exact columns and rebuild positions with currency-safe basis.

    Impossible sells are quarantined while their source records remain in the
    audit trail; mixed-currency positions persist a NULL scalar basis.
    """
    import_retrieved_at = normalize_retrieved_at(retrieved_at) if retrieved_at is not None else utc_now()
    raw = path.read_bytes()
    file_hash = hashlib.sha256(raw).hexdigest()
    existing = connection.execute(
        "SELECT id FROM imports WHERE file_hash = ? AND source_name = ? AND source_identifier = ?",
        (file_hash, source_name, path.name),
    ).fetchone()
    if existing is not None:
        return ImportResult(0, 0, int(existing[0]))
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)):
            raise CsvImportError("ambiguous CSV mapping: duplicate column names")
        if tuple(headers) != EXPECTED_COLUMNS:
            missing = sorted(set(EXPECTED_COLUMNS) - set(headers))
            extra = sorted(set(headers) - set(EXPECTED_COLUMNS))
            raise CsvImportError(f"CSV mapping rejected; missing={missing}, extra={extra}")
        import_id = int(connection.execute(
            """
            INSERT INTO imports(file_hash, source_name, source_identifier, imported_at)
            VALUES (?, ?, ?, ?) RETURNING id
            """,
            (file_hash, source_name, path.name, import_retrieved_at),
        ).fetchone()[0])
        accepted = 0
        warnings = 0
        pending_rows: dict[str, tuple[int, dict[str, str | list[str] | None]]] = {}
        for row_number, row in enumerate(reader, 2):
            row_identity = hashlib.sha256(
                _row_json(row).encode()
            ).hexdigest()
            connection.execute("SAVEPOINT import_row")
            try:
                if row.get(None):
                    raise ValueError("extra CSV fields are not allowed")
                date.fromisoformat(row["取引日"])
                account_type = ACCOUNT_ALIASES.get(row["口座区分"], "unknown_review")
                transaction_type = row["取引区分"]
                if account_type == "unknown_review":
                    raise ValueError("unknown account is retained for review")
                if transaction_type not in {"BUY", "SELL", "DISTRIBUTION", "CASH"}:
                    raise ValueError("unsupported transaction")
                quantity = _float(row["数量"], field="数量", required=transaction_type != "CASH")
                price = _float(
                    row["単価"], field="単価",
                    required=transaction_type in {"BUY", "SELL", "CASH"},
                )
                fee = _float(row["手数料"], field="手数料", required=False)
                distribution = _float(
                    row["分配金"], field="分配金", required=transaction_type == "DISTRIBUTION"
                )
                _security_values_are_valid(transaction_type, price, fee, distribution)
                currency = row["通貨"]
                if not currency:
                    raise ValueError("currency is required")
                if transaction_type == "CASH":
                    account_id = _account_id(connection, account_type)
                    source_id = add_source_record(
                        connection, source_name=source_name,
                        source_identifier=f"{path.name}#row-{row_number}",
                        retrieved_at=import_retrieved_at, observation_date=row["取引日"],
                        instrument_identifier=None, instrument_identifier_type=None,
                        field="cash_movement", value=row["単価"], unit="total_cash",
                        currency=currency, freshness_status="observed",
                        citation_location=f"{path.name}:row-{row_number}",
                        parser_version=PARSER_VERSION,
                    )
                    connection.execute(
                        "INSERT INTO cash_movements(import_id, account_id, movement_date, amount, currency, source_record_id, row_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (import_id, account_id, row["取引日"], price, currency, source_id, row_identity),
                    )
                else:
                    if (
                        not row["銘柄コード"] or not row["銘柄コード種別"]
                        or quantity <= 0
                    ):
                        raise ValueError("security transaction requires a positive quantity and code")
                    account_id = _account_id(connection, account_type)
                    instrument_id = _instrument(
                        connection, row["銘柄コード"], row["銘柄コード種別"],
                        row["銘柄名"], currency,
                    )
                    source_id = add_source_record(
                        connection, source_name=source_name,
                        source_identifier=f"{path.name}#row-{row_number}",
                        retrieved_at=import_retrieved_at, observation_date=row["取引日"],
                        instrument_identifier=row["銘柄コード"],
                        instrument_identifier_type=row["銘柄コード種別"],
                        field=transaction_type.lower(),
                        value=row["分配金"] if transaction_type == "DISTRIBUTION" else row["単価"],
                        unit="total_cash" if transaction_type == "DISTRIBUTION" else "price",
                        currency=currency, freshness_status="observed",
                        citation_location=f"{path.name}:row-{row_number}",
                        parser_version=PARSER_VERSION,
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
                    pending_rows[source_id] = (row_number, row)
                connection.execute("RELEASE SAVEPOINT import_row")
                accepted += 1
            except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
                warnings += 1
                connection.execute("ROLLBACK TO SAVEPOINT import_row")
                connection.execute("RELEASE SAVEPOINT import_row")
                _quarantine(
                    connection, import_id=import_id, row_number=row_number,
                    row=row, reason=str(exc),
                    warning_code="UNKNOWN_ACCOUNT" if row.get("口座区分") not in ACCOUNT_ALIASES else "MALFORMED_ROW",
                    created_at=import_retrieved_at,
                )
        invalid_rows = _rebuild_positions(connection)
        for invalid, reason, warning_code in invalid_rows:
            pending = pending_rows.get(invalid["source_record_id"])
            if pending is None:
                continue
            row_number, row = pending
            connection.execute("DELETE FROM transactions WHERE id = ?", (invalid["id"],))
            _quarantine(
                connection, import_id=import_id, row_number=row_number, row=row,
                reason=reason, warning_code=warning_code,
                created_at=import_retrieved_at,
            )
            accepted -= 1
            warnings += 1
        if invalid_rows:
            _rebuild_positions(connection)
        connection.execute(
            "UPDATE imports SET accepted_rows = ?, warning_count = ? WHERE id = ?",
            (accepted, warnings, import_id),
        )
        connection.commit()
    return ImportResult(accepted, warnings, import_id)
