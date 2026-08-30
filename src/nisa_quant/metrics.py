"""Conservative point-in-time portfolio, price, and risk calculations.

The local policy is deliberately conservative: a fact participates in a
snapshot only when both its observation/fact date and its source retrieval
date are on or before the requested cutoff.  Same-date source refreshes are
deduplicated; disagreement makes that date unavailable for calculations.
Reported data cutoffs use only observations accepted by those checks and the
field-specific metadata/value rules; retained audit rows do not advance them.
Malformed source, transaction, and movement dates are retained for audit but
excluded before any cutoff comparison or age/history arithmetic.
Snapshot replay independently reconciles every SELL against same-currency
quantity and cost basis, skipping legacy rows with invalid values or no
reconciled same-currency lot.  A linked ledger source must use the exact
transaction field, have usable provenance, and be observed no later than the
trade date; cash movements use the analogous movement-date policy.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from .sources import (
    parse_retrieved_at, safe_add, safe_divide, safe_multiply,
    source_fact_chronology_is_valid, source_fact_contract_is_valid,
    source_fact_is_usable,
)
from .identity import fact_conflict_source_ids, resolve_typed_identity, typed_identity_matches
from .watchlist import watchlist_as_of


STALE_AFTER_DAYS = 30
RECOGNIZED_ACCOUNTS = {"NISA", "taxable", "general"}
TRANSACTION_SOURCE_FIELDS = {
    "BUY": ("buy", "price"),
    "SELL": ("sell", "price"),
    "DISTRIBUTION": ("distribution", "total_cash"),
}


def _warning(code: str, message: str, *, instrument: str | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "instrument": instrument}


def _parse_date(value: str, *, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be ISO YYYY-MM-DD")
    return parsed


def _retrieval_date(value: str) -> date | None:
    try:
        return parse_retrieved_at(value).date()
    except ValueError:
        return None


def _retrieval_key(row: sqlite3.Row) -> tuple[datetime, str]:
    try:
        retrieved = parse_retrieved_at(row["retrieved_at"])
    except ValueError:
        retrieved = datetime.min.replace(tzinfo=timezone.utc)
    return retrieved, row["id"]


def _available_by(row: sqlite3.Row, cutoff: str) -> bool:
    cutoff_date = _parse_date(cutoff, field="as_of")
    observation = _date_or_none(row["observation_date"])
    retrieval_date = _retrieval_date(row["retrieved_at"])
    return (
        observation is not None
        and observation <= cutoff_date
        and retrieval_date is not None
        and retrieval_date <= cutoff_date
        and source_fact_chronology_is_valid(row)
    )


def _date_or_none(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _transaction_source_is_valid(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    cutoff_date: date,
) -> bool:
    expected = TRANSACTION_SOURCE_FIELDS.get(row["transaction_type"])
    trade_date = _date_or_none(row["trade_date"])
    source_observation_date = _date_or_none(row["source_observation_date"])
    if expected is None or trade_date is None or source_observation_date is None:
        return False
    if source_observation_date > trade_date or source_observation_date > cutoff_date:
        return False
    try:
        retrieved_at = parse_retrieved_at(row["source_retrieved_at"])
    except ValueError:
        return False
    if retrieved_at.date() < source_observation_date or retrieved_at.date() > cutoff_date:
        return False
    if not source_fact_is_usable(row):
        return False
    expected_field, expected_unit = expected
    if row["source_field"] != expected_field or row["source_unit"] != expected_unit:
        return False
    if not row["currency"] or row["currency"] != row["source_currency"]:
        return False
    if not typed_identity_matches(
        connection,
        row["source_instrument_identifier_type"],
        row["source_instrument_identifier"],
        row["ledger_identifier_type"],
        row["ledger_identifier_value"],
    ):
        return False
    represented_value = row["distribution"] if row["transaction_type"] == "DISTRIBUTION" else row["price"]
    try:
        represented_value = float(represented_value)
        source_value = float(row["value"])
    except (TypeError, ValueError):
        return False
    return math.isfinite(represented_value) and math.isclose(
        represented_value, source_value, rel_tol=0.0, abs_tol=1e-9,
    )


def _cash_source_is_valid(row: sqlite3.Row, *, cutoff_date: date) -> bool:
    movement_date = _date_or_none(row["movement_date"])
    source_observation_date = _date_or_none(row["source_observation_date"])
    if movement_date is None or source_observation_date is None:
        return False
    if source_observation_date > movement_date or source_observation_date > cutoff_date:
        return False
    try:
        retrieved_at = parse_retrieved_at(row["source_retrieved_at"])
        amount = float(row["amount"])
        source_value = float(row["value"])
    except (TypeError, ValueError):
        return False
    if retrieved_at.date() < source_observation_date or retrieved_at.date() > cutoff_date:
        return False
    if not math.isfinite(amount) or not math.isclose(amount, source_value, rel_tol=0.0, abs_tol=1e-9):
        return False
    return (
        source_fact_is_usable(row)
        and row["source_field"] == "cash_movement"
        and row["source_unit"] == "total_cash"
        and bool(row["currency"])
        and row["currency"] == row["source_currency"]
        and row["source_instrument_identifier"] is None
        and row["source_instrument_identifier_type"] is None
    )


def _instrument(connection: sqlite3.Connection, instrument_id: int) -> sqlite3.Row:
    return connection.execute(
        "SELECT * FROM instruments WHERE id = ?", (instrument_id,)
    ).fetchone()


def _series_metrics(values: list[float]) -> dict[str, float | None]:
    if len(values) < 2:
        return {"max_drawdown_pct": None, "volatility_pct": None}
    if not all(math.isfinite(value) for value in values):
        return {"max_drawdown_pct": None, "volatility_pct": None}
    peak = values[0]
    max_drawdown = 0.0
    returns: list[float] = []
    for previous, current in zip(values, values[1:]):
        if previous <= 0 or not math.isfinite(previous) or not math.isfinite(current):
            return {"max_drawdown_pct": None, "volatility_pct": None}
        current_return = safe_divide(current, previous)
        if current_return is None:
            return {"max_drawdown_pct": None, "volatility_pct": None}
        current_return -= 1
        if not math.isfinite(current_return):
            return {"max_drawdown_pct": None, "volatility_pct": None}
        returns.append(current_return)
        peak = max(peak, current)
        drawdown = safe_divide(current, peak)
        if drawdown is None:
            return {"max_drawdown_pct": None, "volatility_pct": None}
        max_drawdown = min(max_drawdown, drawdown - 1)
    if len(returns) < 2:
        volatility = None
    else:
        return_total = safe_add(*returns)
        mean = safe_divide(return_total, float(len(returns))) if return_total is not None else None
        if mean is None:
            return {"max_drawdown_pct": None, "volatility_pct": None}
        variance_total = safe_add(*((value - mean) ** 2 for value in returns))
        variance = safe_divide(variance_total, float(len(returns) - 1)) if variance_total is not None else None
        if variance is None or variance < 0:
            return {"max_drawdown_pct": None, "volatility_pct": None}
        volatility = math.sqrt(variance) * math.sqrt(252) * 100
        if not math.isfinite(volatility):
            volatility = None
    drawdown_pct = max_drawdown * 100
    return {
        "max_drawdown_pct": drawdown_pct if math.isfinite(drawdown_pct) else None,
        "volatility_pct": volatility,
    }


def _source_rows(
    connection: sqlite3.Connection,
    *,
    as_of: str,
    field: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
    include_unusable: bool = False,
) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT * FROM source_records WHERE field = ? OR ? IS NULL ORDER BY observation_date, id",
        (field, field),
    ).fetchall()
    available: list[sqlite3.Row] = []
    for row in rows:
        observation = _date_or_none(row["observation_date"])
        retrieval = _retrieval_date(row["retrieved_at"])
        if row["observation_date"] is not None and observation is None:
            if warnings is not None:
                warnings.append(_warning(
                    "INVALID_SOURCE_DATE",
                    "source observation date is malformed; source excluded from calculations",
                    instrument=row["instrument_identifier"],
                ))
            continue
        if retrieval is None:
            if warnings is not None:
                warnings.append(_warning(
                    "INVALID_SOURCE_DATE",
                    "source retrieval date is malformed; source excluded from calculations",
                    instrument=row["instrument_identifier"],
                ))
            continue
        if observation is not None and not source_fact_chronology_is_valid(row):
            if warnings is not None:
                warnings.append(_warning(
                    "INVALID_SOURCE_DATE",
                    "source retrieval date precedes observation date; source excluded from calculations",
                    instrument=row["instrument_identifier"],
                ))
            continue
        contract_valid = source_fact_contract_is_valid(row)
        if not contract_valid:
            if warnings is not None:
                warnings.append(_warning(
                    "INVALID_SOURCE_CONTRACT",
                    "source fact has an unsupported field, unit, value, currency, parser, or typed identity; source excluded from calculations",
                    instrument=row["instrument_identifier"],
                ))
            continue
        if _available_by(row, as_of):
            available.append(row)
    return available


def _conflicting_source_ids(
    connection: sqlite3.Connection,
    *,
    as_of: str,
) -> set[str]:
    """Return available contract-valid facts in ambiguous identity/date groups."""
    cutoff_date = _parse_date(as_of, field="as_of")
    conflicting_ids = fact_conflict_source_ids(connection, as_of=as_of)
    cash_groups: dict[tuple[object, ...], list[sqlite3.Row]] = {}
    for row in connection.execute(
        "SELECT * FROM source_records WHERE field = 'cash_movement' ORDER BY id",
    ):
        if not source_fact_contract_is_valid(row):
            continue
        observation = _date_or_none(row["observation_date"])
        retrieval = _retrieval_date(row["retrieved_at"])
        if (
            observation is None or retrieval is None or observation > cutoff_date
            or retrieval > cutoff_date or not source_fact_chronology_is_valid(row)
            or not _source_is_bound_to_usable_ledger_fact(connection, row, cutoff_date=cutoff_date)
        ):
            continue
        event_rows = connection.execute(
            "SELECT id FROM cash_movements WHERE source_record_id = ?",
            (row["id"],),
        ).fetchall()
        for event_id in ([event[0] for event in event_rows] or [row["id"]]):
            cash_groups.setdefault((row["observation_date"], event_id), []).append(row)
    for rows in cash_groups.values():
        values = {(float(row["value"]), row["unit"], row["currency"]) for row in rows}
        if len(values) > 1:
            conflicting_ids.update(row["id"] for row in rows)
    return conflicting_ids


def _source_is_bound_to_usable_ledger_fact(
    connection: sqlite3.Connection,
    source: sqlite3.Row,
    *,
    cutoff_date: date,
) -> bool:
    if source["field"] == "cash_movement":
        rows = connection.execute(
            """
            SELECT cm.*, a.account_type, sr.source_name AS source_name,
                   sr.source_url_or_identifier AS source_url_or_identifier,
                   sr.citation_location AS citation_location,
                   sr.retrieved_at AS source_retrieved_at,
                   sr.observation_date AS source_observation_date,
                   sr.field AS field, sr.field AS source_field,
                   sr.value AS source_value, sr.value AS value,
                   sr.unit AS source_unit, sr.unit AS unit,
                   sr.currency AS source_currency, sr.currency AS currency,
                   sr.freshness_status AS freshness_status,
                   sr.parser_version AS parser_version,
                   sr.instrument_identifier AS source_instrument_identifier,
                   sr.instrument_identifier_type AS source_instrument_identifier_type
            FROM cash_movements cm
            JOIN accounts a ON a.id = cm.account_id
            JOIN source_records sr ON sr.id = cm.source_record_id
            WHERE cm.source_record_id = ?
            """,
            (source["id"],),
        ).fetchall()
        return any(
            _date_or_none(row["movement_date"]) is not None
            and _date_or_none(row["movement_date"]) <= cutoff_date
            and _cash_source_is_valid(row, cutoff_date=cutoff_date)
            for row in rows
        )

    rows = connection.execute(
        """
        SELECT t.*, a.account_type, sr.source_name AS source_name,
               sr.source_url_or_identifier AS source_url_or_identifier,
               sr.citation_location AS citation_location,
               sr.retrieved_at AS source_retrieved_at,
               sr.observation_date AS source_observation_date,
               sr.field AS field, sr.field AS source_field,
               sr.value AS source_value, sr.value AS value,
               sr.unit AS source_unit, sr.unit AS unit,
               sr.currency AS source_currency, sr.currency AS currency,
               sr.freshness_status AS freshness_status,
               sr.parser_version AS parser_version,
               sr.instrument_identifier AS source_instrument_identifier,
               sr.instrument_identifier_type AS source_instrument_identifier_type,
               i.identifier_value AS ledger_identifier_value,
               i.identifier_type AS ledger_identifier_type
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        JOIN source_records sr ON sr.id = t.source_record_id
        JOIN instruments i ON i.id = t.instrument_id
        WHERE t.source_record_id = ?
        """,
        (source["id"],),
    ).fetchall()
    for row in rows:
        trade_date = _date_or_none(row["trade_date"])
        if trade_date is None or trade_date > cutoff_date:
            continue
        try:
            values = tuple(float(row[field]) for field in ("quantity", "price", "fee", "distribution"))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in values) or row["fee"] < 0 or row["distribution"] < 0:
            continue
        if row["transaction_type"] in {"BUY", "SELL"} and (row["quantity"] <= 0 or row["price"] <= 0):
            continue
        if _transaction_source_is_valid(connection, row, cutoff_date=cutoff_date):
            return True
    return False


def _historical_watchlist_metadata_exists(
    connection: sqlite3.Connection,
    *,
    identifier_type: str,
    identifier_value: str,
) -> bool:
    return connection.execute(
        "SELECT 1 FROM watchlist_versions WHERE identifier_type = ? AND identifier_value = ? LIMIT 1",
        (identifier_type, identifier_value),
    ).fetchone() is not None


def _position_ledger(
    connection: sqlite3.Connection,
    *,
    as_of: str,
    warnings: list[dict[str, Any]],
    conflicting_source_ids: set[str] | None = None,
) -> tuple[dict[tuple[int, int], dict[str, float]], float | None, list[str], dict[str, float] | None, dict[str, float], dict[str, float] | None, dict[str, float] | None, dict[tuple[int, int], set[str]]]:
    state: dict[tuple[int, int], dict[str, float]] = {}
    currency_state: dict[tuple[int, int, str], dict[str, float]] = {}
    realized_by_currency: dict[str, float] | None = {}
    transaction_source_ids: list[str] = []
    distributions_by_currency: dict[str, float] | None = {}
    transaction_currencies: dict[str, float] = {}
    position_currencies: dict[tuple[int, int], set[str]] = {}
    rows = connection.execute(
        """
        SELECT t.*, a.account_type, sr.source_name AS source_name,
               sr.source_url_or_identifier AS source_url_or_identifier,
               sr.citation_location AS citation_location,
               sr.retrieved_at AS source_retrieved_at,
               sr.observation_date AS source_observation_date,
               sr.field AS field, sr.field AS source_field, sr.value AS source_value,
               sr.value AS value, sr.unit AS source_unit, sr.currency AS source_currency,
               sr.unit AS unit, sr.freshness_status AS freshness_status,
               sr.freshness_status AS source_freshness_status,
               sr.parser_version AS parser_version,
               sr.instrument_identifier AS source_instrument_identifier,
               sr.instrument_identifier_type AS source_instrument_identifier_type,
               i.identifier_value AS ledger_identifier_value,
               i.identifier_type AS ledger_identifier_type
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        JOIN source_records sr ON sr.id = t.source_record_id
        JOIN instruments i ON i.id = t.instrument_id
        WHERE a.account_type IN ('NISA', 'taxable', 'general')
        ORDER BY t.trade_date, t.id
        """,
    ).fetchall()
    cutoff_date = _parse_date(as_of, field="as_of")
    for row in rows:
        if conflicting_source_ids and row["source_record_id"] in conflicting_source_ids:
            warnings.append(_warning(
                "CONFLICTING_SOURCE_FACT",
                "conflicting source fact is retained for audit but excluded from accounting",
                instrument=row["ledger_identifier_value"],
            ))
            continue
        trade_date = _date_or_none(row["trade_date"])
        if trade_date is None:
            warnings.append(_warning(
                "INVALID_TRANSACTION_SOURCE",
                "transaction trade date is malformed; linked source cannot authorize accounting",
                instrument=row["ledger_identifier_value"],
            ))
            continue
        if trade_date > cutoff_date:
            continue
        try:
            quantity = float(row["quantity"])
            price = float(row["price"])
            fee = float(row["fee"])
            distribution = float(row["distribution"])
        except (TypeError, ValueError):
            quantity = price = fee = distribution = math.nan
        valid_values = all(math.isfinite(value) for value in (quantity, price, fee, distribution))
        if fee < 0 or distribution < 0:
            valid_values = False
        if row["transaction_type"] in {"BUY", "SELL"} and (quantity <= 0 or price <= 0):
            valid_values = False
        if not valid_values:
            warnings.append(_warning(
                "INVALID_TRANSACTION_VALUES",
                "BUY/SELL price and fee values must be finite, positive, and non-negative respectively; accounting skipped",
                instrument=row["ledger_identifier_value"],
            ))
            continue
        if not _transaction_source_is_valid(connection, row, cutoff_date=cutoff_date):
            warnings.append(_warning(
                "INVALID_TRANSACTION_SOURCE",
                "linked transaction source is missing a usable matching field, value, identity, currency, or chronology; accounting skipped",
                instrument=row["ledger_identifier_value"],
            ))
            continue
        currency = row["currency"]
        if row["transaction_type"] == "DISTRIBUTION":
            transaction_currencies[currency] = transaction_currencies.get(currency, 0.0) + 1
            if distributions_by_currency is None:
                continue
            distribution_value = safe_add(
                distributions_by_currency.get(currency, 0.0), row["distribution"],
            )
            if distribution_value is None:
                distributions_by_currency = None
                warnings.append(_warning(
                    "NONFINITE_DERIVED_VALUE",
                    "distribution aggregate overflowed; transaction accounting skipped",
                    instrument=row["ledger_identifier_value"],
                ))
                continue
            transaction_source_ids.append(row["source_record_id"])
            distributions_by_currency[currency] = distribution_value
            continue
        key = (row["account_id"], row["instrument_id"])
        position = state.setdefault(key, {"quantity": 0.0, "cost_basis": 0.0})
        currency_position = currency_state.setdefault(
            (row["account_id"], row["instrument_id"], currency), {"quantity": 0.0, "cost_basis": 0.0},
        )
        if row["transaction_type"] == "BUY":
            transaction_cost = safe_add(
                safe_multiply(row["quantity"], row["price"]) or math.inf,
                row["fee"],
            )
            if transaction_cost is None:
                warnings.append(_warning(
                    "NONFINITE_DERIVED_VALUE",
                    "transaction cost basis overflowed; transaction accounting skipped",
                    instrument=row["ledger_identifier_value"],
                ))
                continue
            next_quantity = safe_add(position["quantity"], row["quantity"])
            next_cost_basis = safe_add(position["cost_basis"], transaction_cost)
            next_currency_quantity = safe_add(currency_position["quantity"], row["quantity"])
            next_currency_cost = safe_add(currency_position["cost_basis"], transaction_cost)
            if None in (next_quantity, next_cost_basis, next_currency_quantity, next_currency_cost):
                warnings.append(_warning(
                    "NONFINITE_DERIVED_VALUE",
                    "position arithmetic overflowed; transaction accounting skipped",
                    instrument=row["ledger_identifier_value"],
                ))
                continue
            transaction_source_ids.append(row["source_record_id"])
            transaction_currencies[currency] = transaction_currencies.get(currency, 0.0) + 1
            position_currencies.setdefault(key, set()).add(currency)
            position["quantity"] = next_quantity
            position["cost_basis"] = next_cost_basis
            currency_position["quantity"] = next_currency_quantity
            currency_position["cost_basis"] = next_currency_cost
        else:
            if (
                not math.isfinite(row["quantity"])
                or row["quantity"] <= 0
                or not math.isfinite(currency_position["quantity"])
                or not math.isfinite(currency_position["cost_basis"])
                or currency_position["quantity"] <= 0
                or currency_position["cost_basis"] < 0
                or row["quantity"] > currency_position["quantity"]
            ):
                warnings.append(_warning(
                    "UNRECONCILED_SELL",
                    "SELL has no sufficient same-currency quantity and cost basis; accounting skipped",
                    instrument=row["ledger_identifier_value"],
                ))
                continue
            average_basis = safe_divide(currency_position["cost_basis"], currency_position["quantity"])
            sold_basis = safe_multiply(row["quantity"], average_basis) if average_basis is not None else None
            proceeds = safe_multiply(row["quantity"], row["price"])
            realized = safe_add(proceeds or math.inf, -row["fee"], -(sold_basis or math.inf))
            next_position_quantity = safe_add(position["quantity"], -row["quantity"])
            next_position_cost = safe_add(position["cost_basis"], -(sold_basis or math.inf))
            next_currency_quantity = safe_add(currency_position["quantity"], -row["quantity"])
            next_currency_cost = safe_add(currency_position["cost_basis"], -(sold_basis or math.inf))
            if (
                sold_basis is None or sold_basis > currency_position["cost_basis"]
                or realized is None or None in (
                    next_position_quantity, next_position_cost,
                    next_currency_quantity, next_currency_cost,
                )
            ):
                warnings.append(_warning(
                    "UNRECONCILED_SELL",
                    "SELL has no sufficient same-currency cost basis; accounting skipped",
                    instrument=row["ledger_identifier_value"],
                ))
                continue
            if realized_by_currency is None:
                warnings.append(_warning(
                    "NONFINITE_DERIVED_VALUE",
                    "realized P/L aggregate is unavailable; SELL accounting skipped",
                    instrument=row["ledger_identifier_value"],
                ))
                continue
            next_realized = (
                safe_add(realized_by_currency.get(currency, 0.0), realized)
            )
            if next_realized is None:
                realized_by_currency = None
                warnings.append(_warning(
                    "NONFINITE_DERIVED_VALUE",
                    "realized P/L overflowed; transaction accounting skipped",
                    instrument=row["ledger_identifier_value"],
                ))
                continue
            transaction_source_ids.append(row["source_record_id"])
            transaction_currencies[currency] = transaction_currencies.get(currency, 0.0) + 1
            position_currencies.setdefault(key, set()).add(currency)
            position["quantity"] = next_position_quantity
            position["cost_basis"] = next_position_cost
            currency_position["quantity"] = next_currency_quantity
            currency_position["cost_basis"] = next_currency_cost
            if realized_by_currency is not None and next_realized is not None:
                realized_by_currency[currency] = next_realized
    cost_basis_by_currency: dict[str, float] = {}
    cost_basis_overflowed = False
    for (_, _, currency), currency_position in currency_state.items():
        if currency_position["cost_basis"]:
            total_cost = safe_add(
                cost_basis_by_currency.get(currency, 0.0), currency_position["cost_basis"],
            )
            if total_cost is not None:
                cost_basis_by_currency[currency] = total_cost
            else:
                cost_basis_overflowed = True
                warnings.append(_warning(
                    "NONFINITE_DERIVED_VALUE",
                    "cost basis aggregate overflowed; aggregate is unavailable",
                ))
                break
    if cost_basis_overflowed:
        cost_basis_by_currency = None
    realized_pl = (
        next(iter(realized_by_currency.values()))
        if realized_by_currency and len(realized_by_currency) == 1
        else None
    )
    return state, realized_pl, transaction_source_ids, distributions_by_currency, transaction_currencies, realized_by_currency, cost_basis_by_currency, position_currencies


def _group_sources(
    rows: list[sqlite3.Row],
    *,
    by_typed_identity: bool = False,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[object, ...], list[sqlite3.Row]] = {}
    for row in rows:
        key = (
            row["observation_date"], row["instrument_identifier_type"],
            row["instrument_identifier"],
        ) if by_typed_identity else (row["observation_date"],)
        grouped.setdefault(key, []).append(row)
    result: list[dict[str, Any]] = []
    for key, candidates in sorted(grouped.items()):
        observation_date = key[0]
        usable_candidates = [
            source for source in candidates
            if source_fact_is_usable(source) and source_fact_chronology_is_valid(source)
        ]
        valid_candidates = [
            source for source in candidates
            if source_fact_contract_is_valid(source) and source_fact_chronology_is_valid(source)
        ]
        values: set[tuple[float, str | None, str | None]] = set()
        for source in valid_candidates:
            try:
                value = float(source["value"])
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.add((value, source["currency"], source["unit"]))
        # Retrieval recency is the authority rule for identical observations;
        # the stable ID is only a tie-breaker for equal retrieval timestamps.
        chosen = max(usable_candidates or candidates, key=_retrieval_key)
        result.append({
            "date": observation_date,
            "source": chosen,
            "sources": candidates,
            "conflict": len(values) > 1,
            "value": next(iter(values))[0] if len(values) == 1 else None,
            "usable": bool(usable_candidates),
            "usable_source_ids": [source["id"] for source in usable_candidates],
        })
    return result


def _watchlist_map(connection: sqlite3.Connection, *, as_of: str) -> dict[tuple[str, str], sqlite3.Row]:
    return {
        (row["identifier_type"], row["identifier_value"]): row
        for row in watchlist_as_of(connection, as_of=as_of)
    }


def _linked_source_rows(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    identifier_type: str,
    identifier_value: str,
) -> list[sqlite3.Row]:
    """Return facts for one identity and its explicitly linked aliases."""
    component = resolve_typed_identity(connection, identifier_type, identifier_value)
    return [
        row for row in rows
        if (row["instrument_identifier_type"], row["instrument_identifier"]) in component
    ]


def _market_data(
    *,
    connection: sqlite3.Connection,
    metadata: dict[str, Any],
    price_rows: list[sqlite3.Row],
    benchmark_rows: list[sqlite3.Row],
    distribution_rows: list[sqlite3.Row],
    conflicting_source_ids: set[str],
    as_of_date: date,
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    identifier = metadata["identifier_value"]
    identifier_type = metadata["identifier_type"]
    groups = _group_sources(price_rows)
    usable_groups = [group for group in groups if group["usable"]]
    conflict = any(
        group["conflict"] or any(source["id"] in conflicting_source_ids for source in group["sources"])
        for group in usable_groups
    )
    currency_mismatch = any(
        source["currency"] != metadata["currency"]
        for source in price_rows
        if source_fact_is_usable(source) and metadata.get("currency") is not None
    )
    if conflict:
        for group in groups:
            if group["conflict"]:
                warnings.append(_warning("CONFLICTING_PRICE", f"Multiple source prices conflict on {group['date']} for {identifier}", instrument=identifier))
    if currency_mismatch:
        warnings.append(_warning("PRICE_CURRENCY_MISMATCH", f"Price currency does not match {identifier}", instrument=identifier))
    valid_groups = [] if conflict or currency_mismatch else [group for group in usable_groups if group["value"] is not None]
    latest = valid_groups[-1] if valid_groups else None
    status = latest["source"]["freshness_status"] if latest else (
        "stale" if any(source["freshness_status"] == "stale" for source in price_rows) else "unavailable"
    )
    if conflict:
        status = "conflicting"
    latest_price = latest["value"] if latest else None
    if latest and latest["date"]:
        age = (as_of_date - _parse_date(latest["date"], field="observation_date")).days
        if age > STALE_AFTER_DAYS and status != "conflicting":
            status = "stale"
            warnings.append(_warning("STALE_PRICE", f"Price for {identifier} is {age} days old", instrument=identifier))
            latest = None
            latest_price = None
            valid_groups = []
    if status == "stale":
        warnings.append(_warning("STALE_PRICE", f"Price for {identifier} is marked stale", instrument=identifier))
    if latest_price is None:
        warnings.append(_warning("MISSING_PRICE", f"No accepted price for {identifier}", instrument=identifier))
    history = [
        {"date": group["date"], "price": group["value"], "source_id": group["source"]["id"]}
        for group in valid_groups
    ]
    source_ids = [group["source"]["id"] for group in valid_groups]
    values = [item["price"] for item in history]
    series = _series_metrics(values)
    if len(values) >= 2 and series["max_drawdown_pct"] is None:
        warnings.append(_warning(
            "NONFINITE_DERIVED_VALUE",
            f"price return or drawdown overflowed; derived metrics are unavailable for {identifier}",
            instrument=identifier,
        ))
    if len(values) >= 3 and series["volatility_pct"] is None:
        warnings.append(_warning(
            "NONFINITE_DERIVED_VALUE",
            f"price volatility overflowed; risk metric is unavailable for {identifier}",
            instrument=identifier,
        ))
    declared_benchmark = metadata.get("benchmark_identifier_value")
    benchmark_type = metadata.get("benchmark_identifier_type")
    if not benchmark_type or not declared_benchmark:
        benchmark_name = None
        warnings.append(_warning("UNDECLARED_BENCHMARK", f"No benchmark is declared for {identifier}", instrument=identifier))
        matching_benchmarks = []
    else:
        benchmark_name = declared_benchmark
        matching_benchmarks = [
            row for row in benchmark_rows
            if typed_identity_matches(
                connection, row["instrument_identifier_type"], row["instrument_identifier"],
                benchmark_type, benchmark_name,
            )
        ]
        if not matching_benchmarks:
            warnings.append(_warning("MISSING_BENCHMARK", f"Declared benchmark {benchmark_name} is unavailable for {identifier}", instrument=identifier))
    benchmark_groups = _group_sources(matching_benchmarks)
    usable_benchmark_groups = [group for group in benchmark_groups if group["usable"]]
    benchmark_conflict = any(
        group["conflict"] or any(source["id"] in conflicting_source_ids for source in group["sources"])
        for group in usable_benchmark_groups
    )
    benchmark_currency_mismatch = any(
        source["currency"] != metadata["currency"]
        for source in matching_benchmarks
        if source_fact_is_usable(source) and metadata.get("currency") is not None
    )
    if benchmark_conflict:
        warnings.append(_warning("CONFLICTING_BENCHMARK", f"Benchmark sources conflict for {identifier}", instrument=identifier))
    if benchmark_currency_mismatch:
        warnings.append(_warning("BENCHMARK_CURRENCY_MISMATCH", f"Benchmark currency does not match {identifier}", instrument=identifier))
    benchmark_valid_groups = [] if benchmark_conflict or benchmark_currency_mismatch else [group for group in usable_benchmark_groups if group["value"] is not None]
    benchmark_status = benchmark_valid_groups[-1]["source"]["freshness_status"] if benchmark_valid_groups else (
        "stale" if any(source["freshness_status"] == "stale" for source in matching_benchmarks) else "unavailable"
    )
    if benchmark_valid_groups and benchmark_valid_groups[-1]["date"]:
        benchmark_age = (as_of_date - _parse_date(benchmark_valid_groups[-1]["date"], field="observation_date")).days
        if benchmark_age > STALE_AFTER_DAYS:
            benchmark_status = "stale"
            benchmark_valid_groups = []
            warnings.append(_warning("STALE_BENCHMARK", f"Benchmark for {identifier} is {benchmark_age} days old", instrument=identifier))
    elif matching_benchmarks and benchmark_status == "stale":
        warnings.append(_warning("STALE_BENCHMARK", f"Benchmark for {identifier} is marked stale", instrument=identifier))
    if matching_benchmarks and not benchmark_valid_groups and benchmark_status == "unavailable":
        warnings.append(_warning("UNUSABLE_BENCHMARK", f"No usable benchmark observation for {identifier}", instrument=identifier))
    benchmark_values = [group["value"] for group in benchmark_valid_groups]
    price_by_date = {group["date"]: group["value"] for group in valid_groups}
    benchmark_by_date = {group["date"]: group["value"] for group in benchmark_valid_groups}
    common_dates = sorted(set(price_by_date) & set(benchmark_by_date))
    if len(common_dates) >= 2 and price_by_date[common_dates[0]] and benchmark_by_date[common_dates[0]]:
        price_ratio = safe_divide(price_by_date[common_dates[-1]], price_by_date[common_dates[0]])
        benchmark_ratio = safe_divide(benchmark_by_date[common_dates[-1]], benchmark_by_date[common_dates[0]])
        if price_ratio is None or benchmark_ratio is None:
            warnings.append(_warning(
                "NONFINITE_DERIVED_VALUE",
                f"price or benchmark return overflowed for {identifier}",
                instrument=identifier,
            ))
        price_return = (price_ratio - 1) * 100 if price_ratio is not None else None
        benchmark_return = (benchmark_ratio - 1) * 100 if benchmark_ratio is not None else None
        if price_return is not None and not math.isfinite(price_return):
            warnings.append(_warning(
                "NONFINITE_DERIVED_VALUE",
                f"price return overflowed for {identifier}",
                instrument=identifier,
            ))
            price_return = None
        if benchmark_return is not None and not math.isfinite(benchmark_return):
            warnings.append(_warning(
                "NONFINITE_DERIVED_VALUE",
                f"benchmark return overflowed for {identifier}",
                instrument=identifier,
            ))
            benchmark_return = None
    else:
        price_ratio = safe_divide(values[-1], values[0]) if len(values) >= 2 and values[0] else None
        if len(values) >= 2 and values[0] and price_ratio is None:
            warnings.append(_warning(
                "NONFINITE_DERIVED_VALUE",
                f"price return overflowed for {identifier}",
                instrument=identifier,
            ))
        price_return = (price_ratio - 1) * 100 if price_ratio is not None else None
        if price_return is not None and not math.isfinite(price_return):
            warnings.append(_warning(
                "NONFINITE_DERIVED_VALUE",
                f"price return overflowed for {identifier}",
                instrument=identifier,
            ))
            price_return = None
        benchmark_return = None
        if benchmark_valid_groups:
            warnings.append(_warning("MISALIGNED_BENCHMARK", f"Benchmark and instrument returns have fewer than two common dates for {identifier}", instrument=identifier))
    distribution_groups = _group_sources(distribution_rows)
    usable_distribution_groups = [group for group in distribution_groups if group["usable"]]
    distribution_conflict = any(
        group["conflict"] or any(source["id"] in conflicting_source_ids for source in group["sources"])
        for group in usable_distribution_groups
    )
    distribution_currency_mismatch = any(
        source["currency"] != metadata["currency"]
        for source in distribution_rows
        if source_fact_is_usable(source) and metadata.get("currency") is not None
    )
    if distribution_conflict:
        warnings.append(_warning("CONFLICTING_DISTRIBUTION", f"Distribution observations conflict for {identifier}", instrument=identifier))
    if distribution_currency_mismatch:
        warnings.append(_warning("DISTRIBUTION_CURRENCY_MISMATCH", f"Distribution currency does not match {identifier}", instrument=identifier))
    valid_distribution_groups = (
        [] if distribution_conflict or distribution_currency_mismatch
        else [group for group in usable_distribution_groups if group["value"] is not None]
    )
    latest_distribution = valid_distribution_groups[-1] if valid_distribution_groups else None
    if latest_distribution and latest_distribution["date"]:
        distribution_age = (as_of_date - _parse_date(latest_distribution["date"], field="observation_date")).days
        if distribution_age > STALE_AFTER_DAYS:
            latest_distribution = None
            valid_distribution_groups = []
            warnings.append(_warning("STALE_DISTRIBUTION", f"Distribution for {identifier} is {distribution_age} days old", instrument=identifier))
    elif distribution_rows and not valid_distribution_groups and any(source["freshness_status"] == "stale" for source in distribution_rows):
        warnings.append(_warning("STALE_DISTRIBUTION", f"Distribution for {identifier} is marked stale", instrument=identifier))
    if distribution_rows and not valid_distribution_groups and not any(
        warning["code"] == "STALE_DISTRIBUTION" and warning.get("instrument") == identifier for warning in warnings
    ) and not distribution_conflict and not distribution_currency_mismatch:
        warnings.append(_warning("UNUSABLE_DISTRIBUTION", f"No usable distribution observation for {identifier}", instrument=identifier))
    distribution_amount = latest_distribution["value"] if latest_distribution else None
    distribution_change = None
    if len(valid_distribution_groups) >= 2:
        previous_distribution = valid_distribution_groups[-2]["value"]
        if previous_distribution:
            distribution_ratio = safe_divide(distribution_amount, previous_distribution)
            if distribution_ratio is None:
                warnings.append(_warning(
                    "NONFINITE_DERIVED_VALUE",
                    f"distribution return overflowed for {identifier}",
                    instrument=identifier,
                ))
            distribution_change = (
                (distribution_ratio - 1) * 100
                if distribution_ratio is not None
                and math.isfinite((distribution_ratio - 1) * 100)
                else None
            )
            if distribution_change is None and distribution_ratio is not None:
                warnings.append(_warning(
                    "NONFINITE_DERIVED_VALUE",
                    f"distribution return overflowed for {identifier}",
                    instrument=identifier,
                ))
    if metadata.get("asset_type", "").lower() == "etf":
        if not distribution_groups:
            warnings.append(_warning("MISSING_DISTRIBUTION_HISTORY", f"No distribution history for {identifier}", instrument=identifier))
        elif len(valid_distribution_groups) < 2:
            warnings.append(_warning("INSUFFICIENT_DISTRIBUTION_HISTORY", f"Insufficient distribution history for {identifier}", instrument=identifier))
    distribution_yield = None
    if (
        latest_distribution is not None
        and latest_distribution["source"]["unit"] == "per_unit"
        and latest_price is not None
        and latest_price > 0
        and status not in {"conflicting", "unavailable"}
    ):
        distribution_ratio = safe_divide(distribution_amount, latest_price)
        if distribution_ratio is None:
            warnings.append(_warning(
                "NONFINITE_DERIVED_VALUE",
                f"distribution yield overflowed for {identifier}",
                instrument=identifier,
            ))
        distribution_yield = (
            distribution_ratio * 100
            if distribution_ratio is not None and math.isfinite(distribution_ratio * 100)
            else None
        )
        if distribution_yield is None and distribution_ratio is not None:
            warnings.append(_warning(
                "NONFINITE_DERIVED_VALUE",
                f"distribution yield overflowed for {identifier}",
                instrument=identifier,
            ))
    benchmark_relative = None
    if price_return is not None and benchmark_return is not None:
        relative = price_return - benchmark_return
        if math.isfinite(relative):
            benchmark_relative = relative
        else:
            warnings.append(_warning(
                "NONFINITE_DERIVED_VALUE",
                f"benchmark-relative return overflowed for {identifier}",
                instrument=identifier,
            ))
    return {
        "identifier": identifier,
        "identifier_type": identifier_type,
        "latest_price": latest_price,
        "price_date": latest["date"] if latest else None,
        "price_status": status,
        "source_ids": source_ids,
        "price_history": history,
        "max_drawdown_pct": series["max_drawdown_pct"],
        "volatility_pct": series["volatility_pct"],
        "price_return_pct": price_return,
        "benchmark": benchmark_name if benchmark_name is not None else "UNDECLARED",
        "benchmark_identifier_type": benchmark_type,
        "benchmark_identifier_value": benchmark_name,
        "benchmark_instrument": benchmark_valid_groups[-1]["source"]["instrument_identifier"] if benchmark_valid_groups else None,
        "benchmark_price": benchmark_values[-1] if benchmark_values else None,
        "benchmark_price_date": benchmark_valid_groups[-1]["date"] if benchmark_valid_groups else None,
        "benchmark_return_pct": benchmark_return,
        "benchmark_relative_pct": benchmark_relative,
        "benchmark_source_ids": [group["source"]["id"] for group in benchmark_valid_groups],
        "distribution_amount": distribution_amount,
        "distribution_change_pct": distribution_change,
        "distribution_yield_pct": distribution_yield,
        "distribution_date": latest_distribution["date"] if latest_distribution else None,
        "distribution_data_cutoff": latest_distribution["date"] if latest_distribution else None,
        "distribution_unit": latest_distribution["source"]["unit"] if latest_distribution else None,
        "distribution_source_ids": [group["source"]["id"] for group in valid_distribution_groups],
        "distribution_history": [
            {"date": group["date"], "amount": group["value"], "unit": group["source"]["unit"], "source_id": group["source"]["id"]}
            for group in valid_distribution_groups
        ],
    }


def calculate_snapshot(
    connection: sqlite3.Connection,
    *,
    as_of: str,
) -> dict[str, Any]:
    """Calculate a source-bound JSON snapshot without model or network calls."""
    as_of_date = _parse_date(as_of, field="as_of")
    warnings: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT import_id, warning_code, message, row_number, observation_date, created_at FROM data_warnings ORDER BY id"
    ):
        created_date = _retrieval_date(row[5])
        observation_date = _date_or_none(row[4])
        if (
            created_date is None
            or observation_date is None
            or created_date > as_of_date
            or observation_date > as_of_date
            or created_date < observation_date
        ):
            continue
        warnings.append({
            "code": row[1], "message": row[2], "row_number": row[3],
            "import_id": row[0], "observation_date": row[4], "created_at": row[5],
        })

    conflicting_source_ids = _conflicting_source_ids(connection, as_of=as_of)
    state, realized_pl, transaction_source_ids, distributions_by_currency, transaction_currencies, realized_by_currency, cost_basis_by_currency, position_currencies = _position_ledger(
        connection, as_of=as_of, warnings=warnings, conflicting_source_ids=conflicting_source_ids,
    )
    price_sources = _source_rows(connection, as_of=as_of, field="price", warnings=warnings)
    benchmark_sources = _source_rows(connection, as_of=as_of, field="benchmark_price", warnings=warnings)
    distribution_sources = _source_rows(connection, as_of=as_of, field="distribution", warnings=warnings)
    source_currency_by_id = {
        source["id"]: source["currency"]
        for source in (*price_sources, *benchmark_sources, *distribution_sources)
        if source["currency"]
    }
    watchlist_map = _watchlist_map(connection, as_of=as_of)
    holdings: list[dict[str, Any]] = []
    market_value_by_currency: dict[str, float] | None = {}
    market_value_currencies: set[str] = set()
    accepted_price_source_ids: set[str] = set()
    accepted_benchmark_source_ids: set[str] = set()
    accepted_distribution_source_ids: set[str] = set()
    holding_relevant_source_ids: set[str] = set()
    for (account_id, instrument_id), position in sorted(state.items()):
        if position["quantity"] <= 0:
            continue
        account_type = connection.execute("SELECT account_type FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]
        instrument = _instrument(connection, instrument_id)
        key = (instrument["identifier_type"], instrument["identifier_value"])
        watch_item = watchlist_map.get(key)
        historical_metadata_missing = watch_item is None and _historical_watchlist_metadata_exists(
            connection, identifier_type=key[0], identifier_value=key[1],
        )
        holding_currency = (
            watch_item["currency"] if watch_item else instrument["currency"] if not historical_metadata_missing else None
        )
        metadata = {
            "identifier_type": instrument["identifier_type"],
            "identifier_value": instrument["identifier_value"],
            "asset_type": (watch_item["asset_type"] if watch_item else instrument["asset_type"] if not historical_metadata_missing else "unknown"),
            "currency": holding_currency,
            "benchmark": (watch_item["benchmark"] if watch_item else instrument["benchmark"] if not historical_metadata_missing else None),
            "benchmark_identifier_type": (watch_item["benchmark_identifier_type"] if watch_item else instrument["benchmark_identifier_type"] if not historical_metadata_missing else None),
            "benchmark_identifier_value": (watch_item["benchmark_identifier_value"] if watch_item else instrument["benchmark_identifier_value"] if not historical_metadata_missing else None),
        }
        market = _market_data(
            connection=connection,
            metadata=metadata,
            price_rows=_linked_source_rows(
                connection, price_sources,
                identifier_type=key[0], identifier_value=key[1],
            ),
            benchmark_rows=benchmark_sources,
            distribution_rows=_linked_source_rows(
                connection, distribution_sources,
                identifier_type=key[0], identifier_value=key[1],
            ),
            conflicting_source_ids=conflicting_source_ids,
            as_of_date=as_of_date, warnings=warnings,
        )
        accepted_price_source_ids.update(market["source_ids"])
        accepted_benchmark_source_ids.update(market["benchmark_source_ids"])
        accepted_distribution_source_ids.update(market["distribution_source_ids"])
        holding_relevant_source_ids.update(market["source_ids"])
        holding_relevant_source_ids.update(market["benchmark_source_ids"])
        holding_relevant_source_ids.update(market["distribution_source_ids"])
        source_instrument_ids = set(transaction_source_ids)
        ledger_source_ids = [
            source_id for source_id in transaction_source_ids
            if (source := connection.execute("SELECT instrument_identifier, instrument_identifier_type FROM source_records WHERE id = ?", (source_id,)).fetchone()) is not None
            and typed_identity_matches(
                connection,
                source["instrument_identifier_type"], source["instrument_identifier"],
                instrument["identifier_type"], instrument["identifier_value"],
            )
        ]
        del source_instrument_ids
        value = None
        if (
            market["latest_price"] is not None
            and market["price_status"] not in {"conflicting", "stale", "unavailable"}
            and holding_currency is not None
        ):
            value = safe_multiply(market["latest_price"], position["quantity"])
            if value is None:
                market_value_by_currency = None
                warnings.append(_warning(
                    "NONFINITE_DERIVED_VALUE",
                    "holding market value overflowed; value and dependent aggregates are unavailable",
                    instrument=instrument["identifier_value"],
                ))
        if value is not None and holding_currency is not None:
            market_value_currencies.add(holding_currency)
            if market_value_by_currency is not None:
                aggregate_value = safe_add(
                    market_value_by_currency.get(holding_currency, 0.0), value,
                )
                if aggregate_value is None:
                    market_value_by_currency = None
                    warnings.append(_warning(
                        "NONFINITE_DERIVED_VALUE",
                        "portfolio market value overflowed; aggregate is unavailable",
                    ))
                else:
                    market_value_by_currency[holding_currency] = aggregate_value
        holding = {
            "instrument": instrument["identifier_value"],
            "identifier_type": instrument["identifier_type"],
            "display_name": watch_item["display_name"] if watch_item else instrument["display_name"] if not historical_metadata_missing else "unavailable",
            "asset_type": (watch_item["asset_type"] if watch_item else instrument["asset_type"] if not historical_metadata_missing else "unknown"),
            "market": (watch_item["market"] if watch_item else instrument["market"] if not historical_metadata_missing else None),
            "currency": holding_currency,
            "metadata_status": "unavailable" if historical_metadata_missing else "available",
            "account": account_type,
            "quantity": position["quantity"],
            "cost_basis": position["cost_basis"] if len(position_currencies.get((account_id, instrument_id), set())) <= 1 else None,
            "market_value": value,
            "ledger_source_ids": ledger_source_ids,
            **market,
        }
        holdings.append(holding)

    all_cash = connection.execute(
        """
        SELECT cm.*, a.account_type, sr.source_name AS source_name,
               sr.source_url_or_identifier AS source_url_or_identifier,
               sr.citation_location AS citation_location,
               sr.retrieved_at AS source_retrieved_at,
               sr.observation_date AS source_observation_date,
               sr.field AS field, sr.field AS source_field, sr.value AS source_value,
               sr.value AS value, sr.unit AS source_unit, sr.currency AS source_currency,
               sr.unit AS unit, sr.freshness_status AS freshness_status,
               sr.freshness_status AS source_freshness_status,
               sr.parser_version AS parser_version,
               sr.instrument_identifier AS source_instrument_identifier,
               sr.instrument_identifier_type AS source_instrument_identifier_type
        FROM cash_movements cm JOIN accounts a ON a.id = cm.account_id
        JOIN source_records sr ON sr.id = cm.source_record_id
        WHERE a.account_type IN ('NISA', 'taxable', 'general', 'cash', 'foreign_currency')
        """,
    ).fetchall()
    cash_by_currency: dict[str, float] | None = {}
    cash_currencies: set[str] = set()
    cash_source_ids: list[str] = []
    accepted_cash: list[sqlite3.Row] = []
    for row in all_cash:
        if conflicting_source_ids and row["source_record_id"] in conflicting_source_ids:
            warnings.append(_warning(
                "CONFLICTING_SOURCE_FACT",
                "conflicting source fact is retained for audit but excluded from cash accounting",
            ))
            continue
        movement_date = _date_or_none(row["movement_date"])
        if movement_date is None:
            warnings.append(_warning(
                "INVALID_CASH_MOVEMENT_SOURCE",
                "cash movement date is malformed; linked source cannot authorize accounting",
            ))
            continue
        if movement_date > as_of_date:
            continue
        if not _cash_source_is_valid(row, cutoff_date=as_of_date):
            warnings.append(_warning(
                "INVALID_CASH_MOVEMENT_SOURCE",
                "cash movement requires a usable cash_movement source with matching finite amount, currency, and chronology; accounting skipped",
            ))
            continue
        currency = row["currency"]
        amount = float(row["amount"])
        cash_currencies.add(currency)
        cash_total = (
            safe_add(cash_by_currency.get(currency, 0.0), amount)
            if cash_by_currency is not None else None
        )
        if cash_by_currency is not None and cash_total is None:
            cash_by_currency = None
            warnings.append(_warning(
                "NONFINITE_DERIVED_VALUE",
                "cash movement aggregate overflowed; aggregate is unavailable",
            ))
            continue
        if cash_by_currency is not None and cash_total is not None:
            cash_by_currency[currency] = cash_total
        cash_source_ids.append(row["source_record_id"])
        accepted_cash.append(row)
    cash_values = list(cash_by_currency.values()) if cash_by_currency is not None else []
    contributions_by_currency: dict[str, float] | None = {}
    if cash_by_currency is None:
        contributions_by_currency = None
    else:
        for currency in cash_currencies:
            contribution_total = safe_add(*(
                cash["amount"] for cash in accepted_cash
                if cash["currency"] == currency and cash["amount"] > 0
            ))
            if contribution_total is None:
                contributions_by_currency = None
                warnings.append(_warning(
                    "NONFINITE_DERIVED_VALUE",
                    "contributions aggregate overflowed; aggregate is unavailable",
                ))
                break
            if contribution_total or currency in cash_by_currency:
                contributions_by_currency[currency] = contribution_total
    holding_currencies = {holding["currency"] for holding in holdings}
    unknown_currency = None in holding_currencies
    currencies = (
        (holding_currencies - {None})
        | market_value_currencies
        | cash_currencies
        | set(transaction_currencies)
        | (set(distributions_by_currency) if distributions_by_currency is not None else set())
        | {
            source_currency_by_id[source_id]
            for source_id in holding_relevant_source_ids
            if source_id in source_currency_by_id
        }
    )
    mixed_currency = len(currencies) > 1
    known_values = bool(holdings) and all(holding["market_value"] is not None for holding in holdings)
    if mixed_currency:
        warnings.append(_warning("MIXED_CURRENCY_NO_FX", "Mixed currencies cannot be aggregated without an explicit FX rate and cited FX source."))
    if unknown_currency:
        warnings.append(_warning("HISTORICAL_METADATA_UNAVAILABLE", "A holding has no watchlist metadata available at the requested cutoff; metadata-dependent aggregates are unavailable."))
    if holdings and not known_values:
        warnings.append(_warning("INCOMPLETE_AGGREGATION", "Allocation, concentration, and portfolio totals are unavailable because a required holding lacks a usable value."))
    single_currency = market_value_by_currency is not None and len(market_value_by_currency) <= 1
    aggregate_safe = (
        known_values and not mixed_currency and not unknown_currency and single_currency
        and cost_basis_by_currency is not None
    )
    market_value_total = safe_add(*market_value_by_currency.values()) if aggregate_safe and market_value_by_currency is not None else None
    cost_basis_total = safe_add(*(holding["cost_basis"] for holding in holdings)) if aggregate_safe else None
    market_value = market_value_total
    cost_basis = cost_basis_total
    if aggregate_safe and (market_value_total is None or cost_basis_total is None):
        warnings.append(_warning(
            "NONFINITE_DERIVED_VALUE",
            "portfolio aggregate overflowed; dependent aggregates are unavailable",
        ))
        aggregate_safe = False

    def allocations(field: str) -> dict[str, float] | None:
        if not aggregate_safe or not market_value:
            return None
        amounts: dict[str, float] = {}
        for holding in holdings:
            amount = safe_add(amounts.get(holding[field], 0.0), holding["market_value"])
            if amount is None:
                return None
            amounts[holding[field]] = amount
        result: dict[str, float] = {}
        for key, amount in amounts.items():
            ratio = safe_divide(amount, market_value)
            percentage = ratio * 100 if ratio is not None else None
            if percentage is None or not math.isfinite(percentage):
                return None
            result[key] = percentage
        return result

    concentration_value = None
    if aggregate_safe and market_value:
        concentration_ratios = [safe_divide(holding["market_value"], market_value) for holding in holdings]
        if all(ratio is not None and math.isfinite(ratio) for ratio in concentration_ratios):
            concentration_value = max(ratio * 100 for ratio in concentration_ratios if ratio is not None)

    portfolio_values: list[float] = []
    ordered_dates: list[str] = []
    if holdings and known_values and all(len(holding["price_history"]) >= 2 for holding in holdings) and not mixed_currency:
        common_dates = set(item["date"] for item in holdings[0]["price_history"])
        for holding in holdings[1:]:
            common_dates &= {item["date"] for item in holding["price_history"]}
        ordered_dates = sorted(common_dates)
        if len(ordered_dates) >= 2:
            indexes = {
                (holding["identifier_type"], holding["instrument"]): {
                    item["date"]: item["price"] for item in holding["price_history"]
                }
                for holding in holdings
            }
            weights = [holding["market_value"] for holding in holdings]
            total_weight = safe_add(*(weight or 0.0 for weight in weights))
            if total_weight is None:
                warnings.append(_warning(
                    "NONFINITE_DERIVED_VALUE",
                    "portfolio history weight aggregate overflowed; history and risk metrics are unavailable",
                ))
            elif total_weight and all(weight is not None for weight in weights):
                portfolio_values = [100.0]
                for previous_date, current_date in zip(ordered_dates, ordered_dates[1:]):
                    weighted_returns: list[float] = []
                    for holding, weight in zip(holdings, weights):
                        index = indexes[(holding["identifier_type"], holding["instrument"])]
                        index_ratio = safe_divide(index[current_date], index[previous_date])
                        weight_ratio = safe_divide(weight, total_weight)
                        if index_ratio is None or weight_ratio is None:
                            weighted_returns = []
                            break
                        weighted_returns.append(weight_ratio * (index_ratio - 1))
                    weighted_return = safe_add(*weighted_returns) if weighted_returns else None
                    if weighted_return is None:
                        warnings.append(_warning(
                            "NONFINITE_DERIVED_VALUE",
                            "portfolio return aggregate overflowed; history and risk metrics are unavailable",
                        ))
                    next_value = (
                        safe_multiply(portfolio_values[-1], 1 + weighted_return)
                        if weighted_return is not None else None
                    )
                    if next_value is None:
                        warnings.append(_warning(
                            "NONFINITE_DERIVED_VALUE",
                            "portfolio history value overflowed; history and risk metrics are unavailable",
                        ))
                        portfolio_values = []
                        break
                    portfolio_values.append(next_value)
    portfolio_series = _series_metrics(portfolio_values)
    if portfolio_series["volatility_pct"] is None:
        volatility_reason = "portfolio return series requires at least three common dated observations (two returns)"
    else:
        volatility_reason = None

    watchlist_output: list[dict[str, Any]] = []
    for item in watchlist_map.values():
        key = (item["identifier_type"], item["identifier_value"])
        if any(holding["identifier_type"] == key[0] and holding["instrument"] == key[1] for holding in holdings):
            continue
        metadata = dict(item)
        market = _market_data(
            connection=connection,
            metadata=metadata,
            price_rows=_linked_source_rows(
                connection, price_sources,
                identifier_type=key[0], identifier_value=key[1],
            ),
            benchmark_rows=benchmark_sources,
            distribution_rows=_linked_source_rows(
                connection, distribution_sources,
                identifier_type=key[0], identifier_value=key[1],
            ),
            conflicting_source_ids=conflicting_source_ids,
            as_of_date=as_of_date, warnings=warnings,
        )
        accepted_price_source_ids.update(market["source_ids"])
        accepted_benchmark_source_ids.update(market["benchmark_source_ids"])
        accepted_distribution_source_ids.update(market["distribution_source_ids"])
        watchlist_output.append({**dict(item), **market})
        if len(market["price_history"]) < 3:
            warnings.append(_warning("INSUFFICIENT_HISTORY", f"Insufficient dated price history for watchlist item {item['identifier_value']}", instrument=item["identifier_value"]))

    benchmark_output = []
    benchmark_components: set[frozenset[tuple[str, str]]] = set()
    for source in benchmark_sources:
        component = resolve_typed_identity(
            connection, source["instrument_identifier_type"], source["instrument_identifier"],
        )
        if component in benchmark_components:
            continue
        benchmark_components.add(component)
        component_rows = [
            row for row in benchmark_sources
            if (row["instrument_identifier_type"], row["instrument_identifier"]) in component
        ]
        for group in _group_sources(component_rows):
            if (
                not group["usable"] or group["conflict"]
                or any(candidate["id"] in conflicting_source_ids for candidate in group["sources"])
                or group["value"] is None
            ):
                continue
            if group["date"] and (as_of_date - _parse_date(group["date"], field="observation_date")).days > STALE_AFTER_DAYS:
                continue
            source = group["source"]
            benchmark_output.append({
                "instrument": source["instrument_identifier"],
                "identifier_type": source["instrument_identifier_type"],
                "price": group["value"],
                "observation_date": group["date"],
                "source_id": source["id"],
            })
            accepted_benchmark_source_ids.add(source["id"])
    for holding in holdings:
        if len(holding["price_history"]) < 3:
            warnings.append(_warning("INSUFFICIENT_HISTORY", f"Insufficient dated price history for {holding['instrument']}", instrument=holding["instrument"]))
    sources = [
        dict(row) for row in _source_rows(connection, as_of=as_of, include_unusable=True)
        if row["id"] not in conflicting_source_ids
    ]
    sources_by_id = {source["id"]: source for source in sources}

    def accepted_cutoff(source_ids: set[str]) -> str | None:
        dates = [
            sources_by_id[source_id]["observation_date"]
            for source_id in source_ids
            if source_id in sources_by_id and sources_by_id[source_id]["observation_date"] is not None
        ]
        return max(dates, default=None)

    price_source_ids = [source_id for holding in holdings for source_id in holding["source_ids"]]
    risk_source_ids = sorted(set(price_source_ids))
    ledger_source_set = set(transaction_source_ids) | set(cash_source_ids)
    portfolio_distribution_source_ids = [
        source_id for source_id in transaction_source_ids
        if any(source["id"] == source_id and source["field"] == "distribution" for source in sources)
    ]

    def provenance(derivation: str, source_ids: list[str] | set[str]) -> dict[str, Any]:
        return {"derivation": derivation, "source_ids": sorted(set(source_ids))}

    portfolio_provenance = {
        "market_value": provenance("sum(quantity * accepted latest price) over holdings", set(price_source_ids) | ledger_source_set),
        "cost_basis": provenance("average-cost ledger calculation over accepted transactions", ledger_source_set),
        "cost_basis_by_currency": provenance("average-cost ledger calculation grouped by transaction currency", ledger_source_set),
        "contributions": provenance("sum of positive explicit cash movements", cash_source_ids),
        "distributions": provenance("sum of accepted total_cash distribution records", portfolio_distribution_source_ids),
        "allocation": provenance("market-value weights grouped by account, asset, and currency", set(price_source_ids) | ledger_source_set),
        "concentration": provenance("largest accepted holding market-value weight", set(price_source_ids) | ledger_source_set),
        "drawdown": provenance("max drawdown over the common dated portfolio return series", risk_source_ids),
        "volatility": provenance("sample annualized volatility over the common dated portfolio return series", risk_source_ids),
        "realized_pl_by_currency": provenance("realized average-cost P/L grouped by transaction currency", ledger_source_set),
    }
    distributions = (
        safe_add(*distributions_by_currency.values())
        if distributions_by_currency is not None
        and len(distributions_by_currency) <= 1 and not mixed_currency else None
    )
    cash_movements = (
        safe_add(*cash_values)
        if cash_by_currency is not None and len(cash_by_currency) <= 1 and not mixed_currency else None
    )
    contributions = (
        safe_add(*contributions_by_currency.values())
        if contributions_by_currency is not None
        and len(contributions_by_currency) <= 1 and not mixed_currency else None
    )
    unrealized_pl = safe_add(market_value, -cost_basis) if aggregate_safe and market_value is not None and cost_basis is not None else None
    realized_currency_count = len(transaction_currencies)
    return {
        "as_of": as_of,
        "calculation_version": "metrics-v3",
        "data_cutoffs": {
            "portfolio": as_of,
            "prices": accepted_cutoff(accepted_price_source_ids),
            "benchmark": accepted_cutoff(accepted_benchmark_source_ids),
            "distributions": accepted_cutoff(accepted_distribution_source_ids),
        },
        "portfolio": {
            "market_value": market_value,
            "market_value_by_currency": market_value_by_currency,
            "cost_basis": cost_basis,
            "cost_basis_by_currency": cost_basis_by_currency,
            "contributions": contributions,
            "contributions_by_currency": contributions_by_currency,
            "cash_movements": cash_movements,
            "cash_movements_by_currency": cash_by_currency,
            "distributions": distributions,
            "distributions_by_currency": distributions_by_currency,
            "distribution_unit": "total_cash",
            "realized_pl": realized_pl if realized_currency_count <= 1 and not mixed_currency else None,
            "realized_pl_by_currency": realized_by_currency,
            "unrealized_pl": unrealized_pl,
            "allocation": {"account_pct": allocations("account"), "asset_pct": allocations("asset_type"), "currency_pct": allocations("currency")},
            "concentration": {"largest_holding_pct": concentration_value},
            "drawdown_pct": portfolio_series["max_drawdown_pct"],
            "volatility": portfolio_series["volatility_pct"],
            "volatility_reason": volatility_reason,
            "provenance": portfolio_provenance,
            "risk_series": {
                "dates": ordered_dates,
                "values": portfolio_values,
                "policy": "constant current-value weights over common dated holding observations",
            },
            "holdings": holdings,
        },
        "benchmarks": benchmark_output,
        "watchlist": watchlist_output,
        "warnings": warnings,
        "sources": sources,
        "currency_context": {"currencies": sorted(currencies), "fx_applied": False},
    }
