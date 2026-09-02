"""Append-only, source-bound recommendation records and outcome evaluation.

Persisted recommendation snapshots are strict finite JSON documents: outcome
creation validates their structure and recomputed canonical hash before any
later evidence can be written.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from .portfolio_metrics import calculate_snapshot
from .candidate_screening import LABELS, run_screens
from .instrument_identity import fact_conflict_source_ids, same_date_fact_conflict, typed_identity_matches
from .source_records import (
    FRESHNESS_STATUSES, IDENTIFIER_TYPES, SOURCE_FACT_UNITS,
    SUPPORTED_FACT_FIELDS, parse_retrieved_at, safe_divide,
    source_fact_chronology_is_valid, source_fact_contract_is_valid, source_fact_is_usable,
)


SOURCE_ID_RE = re.compile(r"^SRC-[a-f0-9]{12}$")
METRIC_NUMERIC_FIELDS = {
    "quantity", "cost_basis", "current_price", "position_return_pct",
    "benchmark_price", "momentum_pct", "moving_average_3",
    "benchmark_return_pct", "benchmark_relative_pct", "max_drawdown_pct",
    "volatility_pct", "distribution_amount", "distribution_change_pct",
    "distribution_yield_pct",
}
METRIC_STRING_FIELDS = {
    "price_date", "price_status", "trend_context", "distribution_date",
    "distribution_data_cutoff", "distribution_unit",
}
METRIC_FIELDS = METRIC_NUMERIC_FIELDS | METRIC_STRING_FIELDS
METRIC_STATUS_VALUES = FRESHNESS_STATUSES
METRIC_TREND_VALUES = frozenset({"above moving average", "below moving average", "unavailable"})
METRIC_UNIT_VALUES = frozenset({"per_unit", "total_cash"})
SNAPSHOT_PROVENANCE_FIELDS = frozenset({
    "market_value", "cost_basis", "cost_basis_by_currency", "contributions", "distributions",
    "realized_pl_by_currency", "allocation", "concentration", "drawdown", "volatility",
})


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _date(value: str, *, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be ISO YYYY-MM-DD")
    return parsed


def _canonical(value: Any) -> tuple[str, str]:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()


def _recommendation_contract_hash(
    *,
    data_cutoff: str,
    provider: str,
    template_version: str,
    source_ids: list[str],
    instrument: str,
    label: str,
    metrics: dict[str, Any],
    reason: str,
    risk: str,
    horizon: str,
    invalidation: str,
    snapshot_json: str,
    snapshot_hash: str,
    identifier_type: str,
    identifier_value: str,
    benchmark_identifier_type: str | None,
    benchmark_identifier_value: str | None,
    currency: str | None,
    freshness_status: str | None,
    provider_contract: str | None = None,
    template_contract: str | None = None,
) -> str:
    _, digest = _canonical({
        "data_cutoff": data_cutoff,
        "provider": provider,
        "provider_contract": provider if provider_contract is None else provider_contract,
        "template_version": template_version,
        "template_contract": template_version if template_contract is None else template_contract,
        "source_ids": source_ids,
        "instrument": instrument,
        "label": label,
        "metrics": metrics,
        "reason": reason,
        "risk": risk,
        "horizon": horizon,
        "invalidation": invalidation,
        "snapshot_json": snapshot_json,
        "snapshot_hash": snapshot_hash,
        "identifier_type": identifier_type,
        "identifier_value": identifier_value,
        "benchmark_identifier_type": benchmark_identifier_type,
        "benchmark_identifier_value": benchmark_identifier_value,
        "currency": currency,
        "freshness_status": freshness_status,
    })
    return digest


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"persisted JSON contains non-finite value: {value}")


def _validate_finite_json(value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        try:
            finite = math.isfinite(float(value))
        except (OverflowError, ValueError):
            finite = False
        if not finite:
            raise ValueError("persisted JSON contains a non-finite numeric value")
        return
    if isinstance(value, dict):
        for nested in value.values():
            _validate_finite_json(nested)
    elif isinstance(value, list):
        for nested in value:
            _validate_finite_json(nested)


def _load_persisted_object(raw: object, *, field: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ValueError(f"persisted {field} must be a JSON object")
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"persisted {field} is invalid strict JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"persisted {field} must be a JSON object")
    _validate_finite_json(value)
    return value


def _load_metrics(raw: object) -> dict[str, Any]:
    metrics = _load_persisted_object(raw, field="recommendation metrics")
    if set(metrics) != METRIC_FIELDS:
        raise ValueError("persisted recommendation metrics have an invalid structure")
    for field in METRIC_NUMERIC_FIELDS:
        value = metrics[field]
        if field == "quantity" and value is None:
            raise ValueError(f"persisted recommendation metric {field} is required")
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"persisted recommendation metric {field} is not numeric")
        if not math.isfinite(float(value)):
            raise ValueError(f"persisted recommendation metric {field} is not finite")
    for field in METRIC_STRING_FIELDS:
        value = metrics[field]
        if value is None and field in {"price_date", "distribution_date", "distribution_data_cutoff", "distribution_unit"}:
            continue
        if not isinstance(value, str):
            raise ValueError(f"persisted recommendation metric {field} is not a string")
    for field in ("price_date", "distribution_date", "distribution_data_cutoff"):
        if metrics[field] is not None:
            _snapshot_date(metrics[field], path=f"metrics.{field}")
    if metrics["price_status"] not in METRIC_STATUS_VALUES:
        raise ValueError("persisted recommendation metric price_status is unsupported")
    if metrics["trend_context"] not in METRIC_TREND_VALUES:
        raise ValueError("persisted recommendation metric trend_context is unsupported")
    if metrics["distribution_unit"] is not None and metrics["distribution_unit"] not in METRIC_UNIT_VALUES:
        raise ValueError("persisted recommendation metric distribution_unit is unsupported")
    return metrics


def _snapshot_object(value: object, *, path: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"persisted recommendation snapshot has an invalid {path} structure")
    return value


def _snapshot_string(value: object, *, path: str, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"persisted recommendation snapshot field {path} is not a string")


def _snapshot_timestamp(value: object, *, path: str) -> None:
    _snapshot_string(value, path=path)
    try:
        parse_retrieved_at(value)
    except ValueError as exc:
        raise ValueError(f"persisted recommendation snapshot field {path} is not a timestamp") from exc


def _snapshot_enum(value: object, *, path: str, allowed: frozenset[str]) -> None:
    _snapshot_string(value, path=path)
    if value not in allowed:
        raise ValueError(f"persisted recommendation snapshot field {path} has an unsupported value")


def _snapshot_number(value: object, *, path: str, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"persisted recommendation snapshot field {path} is not numeric")
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, ValueError):
        finite = False
    if not finite:
        raise ValueError(f"persisted recommendation snapshot field {path} is not finite")


def _snapshot_string_list(value: object, *, path: str, source_ids: bool = False) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"persisted recommendation snapshot field {path} is not a string list")
    if source_ids and any(not SOURCE_ID_RE.fullmatch(item) for item in value):
        raise ValueError(f"persisted recommendation snapshot field {path} has an invalid source ID")


def _snapshot_number_map(value: object, *, path: str, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"persisted recommendation snapshot field {path} is not a numeric map")
    for key, nested in value.items():
        if not isinstance(key, str):
            raise ValueError(f"persisted recommendation snapshot field {path} has a non-string key")
        _snapshot_number(nested, path=f"{path}.{key}")


def _snapshot_date(value: object, *, path: str, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"persisted recommendation snapshot field {path} is not a date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"persisted recommendation snapshot field {path} is not a date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"persisted recommendation snapshot field {path} is not a date")


def _validate_snapshot_provenance(value: object, *, path: str) -> None:
    provenance = _snapshot_object(
        value, path=path,
        fields={"derivation", "source_ids"},
    )
    _snapshot_string(provenance["derivation"], path=f"{path}.derivation")
    _snapshot_string_list(provenance["source_ids"], path=f"{path}.source_ids", source_ids=True)


def _validate_snapshot_price_history(value: object, *, path: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"persisted recommendation snapshot field {path} is not a list")
    for index, item in enumerate(value):
        row_path = f"{path}[{index}]"
        item = _snapshot_object(item, path=row_path, fields={"date", "price", "source_id"})
        _snapshot_date(item["date"], path=f"{row_path}.date")
        _snapshot_number(item["price"], path=f"{row_path}.price")
        _snapshot_string(item["source_id"], path=f"{row_path}.source_id")
        if not SOURCE_ID_RE.fullmatch(item["source_id"]):
            raise ValueError(f"persisted recommendation snapshot field {row_path}.source_id is invalid")


def _validate_snapshot_distribution_history(value: object, *, path: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"persisted recommendation snapshot field {path} is not a list")
    for index, item in enumerate(value):
        row_path = f"{path}[{index}]"
        item = _snapshot_object(item, path=row_path, fields={"date", "amount", "unit", "source_id"})
        _snapshot_date(item["date"], path=f"{row_path}.date")
        _snapshot_number(item["amount"], path=f"{row_path}.amount")
        _snapshot_string(item["unit"], path=f"{row_path}.unit")
        _snapshot_string(item["source_id"], path=f"{row_path}.source_id")
        if not SOURCE_ID_RE.fullmatch(item["source_id"]):
            raise ValueError(f"persisted recommendation snapshot field {row_path}.source_id is invalid")


def _validate_snapshot_source(value: object, *, path: str) -> None:
    source = _snapshot_object(
        value, path=path,
        fields={
            "id", "source_name", "source_url_or_identifier", "retrieved_at", "observation_date",
            "instrument_identifier", "field", "value", "unit", "currency", "freshness_status",
            "citation_location", "instrument_identifier_type", "parser_version",
        },
    )
    for field in ("id", "source_name", "source_url_or_identifier", "field", "freshness_status", "citation_location", "parser_version"):
        _snapshot_string(source[field], path=f"{path}.{field}")
    if not SOURCE_ID_RE.fullmatch(source["id"]):
        raise ValueError(f"persisted recommendation snapshot field {path}.id is invalid")
    _snapshot_timestamp(source["retrieved_at"], path=f"{path}.retrieved_at")
    if source["field"] not in SUPPORTED_FACT_FIELDS:
        raise ValueError(f"persisted recommendation snapshot field {path}.field is unsupported")
    if source["unit"] not in SOURCE_FACT_UNITS[source["field"]]:
        raise ValueError(f"persisted recommendation snapshot field {path}.unit is unsupported")
    if source["freshness_status"] not in FRESHNESS_STATUSES:
        raise ValueError(f"persisted recommendation snapshot field {path}.freshness_status is unsupported")
    _snapshot_date(source["observation_date"], path=f"{path}.observation_date")
    for field in ("instrument_identifier", "unit", "currency", "instrument_identifier_type"):
        _snapshot_string(source[field], path=f"{path}.{field}", nullable=True)
    _snapshot_string(source["value"], path=f"{path}.value")
    try:
        numeric_value = float(source["value"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"persisted recommendation snapshot field {path}.value is not numeric") from exc
    if not math.isfinite(numeric_value):
        raise ValueError(f"persisted recommendation snapshot field {path}.value is not finite")
    if source["field"] in {"price", "benchmark_price", "buy", "sell"} and numeric_value <= 0:
        raise ValueError(f"persisted recommendation snapshot field {path}.value is not positive")
    if source["field"] == "distribution" and numeric_value < 0:
        raise ValueError(f"persisted recommendation snapshot field {path}.value is negative")
    if not isinstance(source["currency"], str) or not source["currency"].strip():
        raise ValueError(f"persisted recommendation snapshot field {path}.currency is invalid")
    if source["field"] == "cash_movement":
        if source["instrument_identifier"] is not None or source["instrument_identifier_type"] is not None:
            raise ValueError(f"persisted recommendation snapshot field {path} has an invalid cash identity")
    elif (
        not isinstance(source["instrument_identifier"], str)
        or not source["instrument_identifier"].strip()
        or source["instrument_identifier_type"] not in IDENTIFIER_TYPES
    ):
        raise ValueError(f"persisted recommendation snapshot field {path} has an invalid typed identity")
    if source["observation_date"] is not None:
        try:
            if parse_retrieved_at(source["retrieved_at"]).date() < date.fromisoformat(source["observation_date"]):
                raise ValueError(f"persisted recommendation snapshot field {path} has invalid chronology")
        except ValueError as exc:
            if "invalid chronology" in str(exc):
                raise
            raise ValueError(f"persisted recommendation snapshot field {path} has invalid chronology") from exc


def _validate_snapshot_market_record(value: object, *, path: str, holding: bool) -> None:
    fields = {
        "identifier", "identifier_type", "latest_price", "price_date", "price_status", "source_ids",
        "price_history", "max_drawdown_pct", "volatility_pct", "price_return_pct", "benchmark",
        "benchmark_identifier_type", "benchmark_identifier_value", "benchmark_instrument", "benchmark_price",
        "benchmark_price_date", "benchmark_return_pct", "benchmark_relative_pct", "benchmark_source_ids",
        "distribution_amount", "distribution_change_pct", "distribution_yield_pct", "distribution_date",
        "distribution_data_cutoff", "distribution_unit", "distribution_source_ids", "distribution_history",
    }
    if holding:
        fields |= {
            "display_name", "asset_type", "market", "currency", "metadata_status", "account",
            "instrument", "quantity", "cost_basis", "market_value", "ledger_source_ids",
        }
    else:
        fields |= {
            "id", "identifier_value", "effective_from", "observed_at", "display_name", "asset_type",
            "market", "currency", "notes",
        }
    record = _snapshot_object(value, path=path, fields=fields)
    for field in ("identifier", "benchmark"):
        _snapshot_string(record[field], path=f"{path}.{field}")
    _snapshot_enum(record["identifier_type"], path=f"{path}.identifier_type", allowed=frozenset(IDENTIFIER_TYPES))
    _snapshot_enum(record["price_status"], path=f"{path}.price_status", allowed=FRESHNESS_STATUSES)
    if holding:
        _snapshot_string(record["instrument"], path=f"{path}.instrument")
    for field in ("price_date", "benchmark_identifier_type", "benchmark_identifier_value", "benchmark_instrument", "benchmark_price_date", "distribution_date", "distribution_data_cutoff", "distribution_unit", "market", "currency"):
        _snapshot_string(record[field], path=f"{path}.{field}", nullable=True)
    for field in ("price_date", "benchmark_price_date", "distribution_date", "distribution_data_cutoff"):
        _snapshot_date(record[field], path=f"{path}.{field}", nullable=True)
    for field in ("benchmark_identifier_type",):
        if record[field] is not None and record[field] not in IDENTIFIER_TYPES:
            raise ValueError(f"persisted recommendation snapshot field {path}.{field} is unsupported")
    if record["distribution_unit"] is not None and record["distribution_unit"] not in METRIC_UNIT_VALUES:
        raise ValueError(f"persisted recommendation snapshot field {path}.distribution_unit is unsupported")
    for field in ("latest_price", "max_drawdown_pct", "volatility_pct", "price_return_pct", "benchmark_price", "benchmark_return_pct", "benchmark_relative_pct", "distribution_amount", "distribution_change_pct", "distribution_yield_pct"):
        _snapshot_number(record[field], path=f"{path}.{field}", nullable=True)
    if holding:
        _snapshot_number(record["market_value"], path=f"{path}.market_value", nullable=True)
    _snapshot_string_list(record["source_ids"], path=f"{path}.source_ids", source_ids=True)
    _snapshot_string_list(record["benchmark_source_ids"], path=f"{path}.benchmark_source_ids", source_ids=True)
    _snapshot_string_list(record["distribution_source_ids"], path=f"{path}.distribution_source_ids", source_ids=True)
    _validate_snapshot_price_history(record["price_history"], path=f"{path}.price_history")
    _validate_snapshot_distribution_history(record["distribution_history"], path=f"{path}.distribution_history")
    if holding:
        for field in ("display_name", "asset_type", "account", "metadata_status"):
            _snapshot_string(record[field], path=f"{path}.{field}")
        _snapshot_number(record["quantity"], path=f"{path}.quantity")
        _snapshot_number(record["cost_basis"], path=f"{path}.cost_basis", nullable=True)
        _snapshot_string_list(record["ledger_source_ids"], path=f"{path}.ledger_source_ids", source_ids=True)
    else:
        if isinstance(record["id"], bool) or not isinstance(record["id"], int):
            raise ValueError(f"persisted recommendation snapshot field {path}.id is not an integer")
        for field in ("identifier_value", "effective_from", "observed_at", "display_name", "asset_type", "market", "currency", "notes"):
            _snapshot_string(record[field], path=f"{path}.{field}")
        _snapshot_date(record["effective_from"], path=f"{path}.effective_from")
        _snapshot_timestamp(record["observed_at"], path=f"{path}.observed_at")
        if parse_retrieved_at(record["observed_at"]).date() < date.fromisoformat(record["effective_from"]):
            raise ValueError(f"persisted recommendation snapshot field {path} has invalid chronology")


def _validate_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    snapshot = _snapshot_object(
        value, path="root",
        fields={"as_of", "calculation_version", "data_cutoffs", "portfolio", "benchmarks", "watchlist", "warnings", "sources", "currency_context"},
    )
    _snapshot_date(snapshot["as_of"], path="as_of")
    _snapshot_string(snapshot["calculation_version"], path="calculation_version")
    cutoffs = _snapshot_object(snapshot["data_cutoffs"], path="data_cutoffs", fields={"portfolio", "prices", "benchmark", "distributions"})
    for field in cutoffs:
        _snapshot_date(cutoffs[field], path=f"data_cutoffs.{field}", nullable=True)
    context = _snapshot_object(snapshot["currency_context"], path="currency_context", fields={"currencies", "fx_applied"})
    _snapshot_string_list(context["currencies"], path="currency_context.currencies")
    if not isinstance(context["fx_applied"], bool):
        raise ValueError("persisted recommendation snapshot field currency_context.fx_applied is not boolean")

    portfolio_fields = {
        "market_value", "market_value_by_currency", "cost_basis", "cost_basis_by_currency", "contributions",
        "contributions_by_currency", "cash_movements", "cash_movements_by_currency", "distributions",
        "distributions_by_currency", "distribution_unit", "realized_pl", "realized_pl_by_currency",
        "unrealized_pl", "allocation", "concentration", "drawdown_pct", "volatility", "volatility_reason",
        "provenance", "risk_series", "holdings",
    }
    portfolio = _snapshot_object(snapshot["portfolio"], path="portfolio", fields=portfolio_fields)
    for field in ("market_value", "cost_basis", "contributions", "cash_movements", "distributions", "realized_pl", "unrealized_pl", "drawdown_pct", "volatility"):
        _snapshot_number(portfolio[field], path=f"portfolio.{field}", nullable=True)
    for field in ("market_value_by_currency", "cost_basis_by_currency", "contributions_by_currency", "cash_movements_by_currency", "distributions_by_currency", "realized_pl_by_currency"):
        _snapshot_number_map(portfolio[field], path=f"portfolio.{field}", nullable=True)
    _snapshot_string(portfolio["distribution_unit"], path="portfolio.distribution_unit")
    _snapshot_string(portfolio["volatility_reason"], path="portfolio.volatility_reason", nullable=True)
    allocation = _snapshot_object(portfolio["allocation"], path="portfolio.allocation", fields={"account_pct", "asset_pct", "currency_pct"})
    for field in allocation:
        _snapshot_number_map(allocation[field], path=f"portfolio.allocation.{field}", nullable=True)
    concentration = _snapshot_object(portfolio["concentration"], path="portfolio.concentration", fields={"largest_holding_pct"})
    _snapshot_number(concentration["largest_holding_pct"], path="portfolio.concentration.largest_holding_pct", nullable=True)
    risk_series = _snapshot_object(portfolio["risk_series"], path="portfolio.risk_series", fields={"dates", "values", "policy"})
    _snapshot_string_list(risk_series["dates"], path="portfolio.risk_series.dates")
    if not isinstance(risk_series["values"], list):
        raise ValueError("persisted recommendation snapshot field portfolio.risk_series.values is not a list")
    for index, value in enumerate(risk_series["values"]):
        _snapshot_number(value, path=f"portfolio.risk_series.values[{index}]")
    _snapshot_string(risk_series["policy"], path="portfolio.risk_series.policy")
    if set(portfolio["provenance"]) != SNAPSHOT_PROVENANCE_FIELDS:
        raise ValueError("persisted recommendation snapshot has an invalid portfolio.provenance structure")
    for field in SNAPSHOT_PROVENANCE_FIELDS:
        _validate_snapshot_provenance(portfolio["provenance"][field], path=f"portfolio.provenance.{field}")
    if not isinstance(portfolio["holdings"], list):
        raise ValueError("persisted recommendation snapshot field portfolio.holdings is not a list")
    for index, holding in enumerate(portfolio["holdings"]):
        _validate_snapshot_market_record(holding, path=f"portfolio.holdings[{index}]", holding=True)

    for name in ("benchmarks", "watchlist", "warnings", "sources"):
        if not isinstance(snapshot[name], list):
            raise ValueError(f"persisted recommendation snapshot field {name} is not a list")
    for index, benchmark in enumerate(snapshot["benchmarks"]):
        benchmark_path = f"benchmarks[{index}]"
        benchmark = _snapshot_object(benchmark, path=benchmark_path, fields={"instrument", "identifier_type", "price", "observation_date", "source_id"})
        _snapshot_string(benchmark["instrument"], path=f"{benchmark_path}.instrument")
        _snapshot_string(benchmark["identifier_type"], path=f"{benchmark_path}.identifier_type")
        _snapshot_number(benchmark["price"], path=f"{benchmark_path}.price")
        _snapshot_date(benchmark["observation_date"], path=f"{benchmark_path}.observation_date", nullable=True)
        _snapshot_string_list([benchmark["source_id"]], path=f"{benchmark_path}.source_id", source_ids=True)
    for index, item in enumerate(snapshot["watchlist"]):
        _validate_snapshot_market_record(item, path=f"watchlist[{index}]", holding=False)
    for index, warning in enumerate(snapshot["warnings"]):
        warning_path = f"warnings[{index}]"
        if isinstance(warning, dict) and set(warning) == {"code", "message", "instrument"}:
            _snapshot_string(warning["code"], path=f"{warning_path}.code")
            _snapshot_string(warning["message"], path=f"{warning_path}.message")
            _snapshot_string(warning["instrument"], path=f"{warning_path}.instrument", nullable=True)
        else:
            warning = _snapshot_object(warning, path=warning_path, fields={"code", "message", "row_number", "import_id", "observation_date", "created_at"})
            _snapshot_string(warning["code"], path=f"{warning_path}.code")
            _snapshot_string(warning["message"], path=f"{warning_path}.message")
            for field in ("row_number", "import_id"):
                if warning[field] is not None and (isinstance(warning[field], bool) or not isinstance(warning[field], int)):
                    raise ValueError(f"persisted recommendation snapshot field {warning_path}.{field} is not an integer")
            _snapshot_date(warning["observation_date"], path=f"{warning_path}.observation_date", nullable=True)
            _snapshot_string(warning["created_at"], path=f"{warning_path}.created_at")
    source_ids: set[str] = set()
    source_facts: dict[tuple[object, ...], tuple[object, ...]] = {}
    for index, source in enumerate(snapshot["sources"]):
        _validate_snapshot_source(source, path=f"sources[{index}]")
        if source["id"] in source_ids:
            raise ValueError("persisted recommendation snapshot contains duplicate source IDs")
        source_ids.add(source["id"])
        fact_key = (
            source["field"], source["instrument_identifier_type"],
            source["instrument_identifier"], source["observation_date"],
            source["id"] if source["field"] in {"buy", "sell", "cash_movement"} else None,
        )
        fact_value = (source["value"], source["unit"], source["currency"])
        prior_value = source_facts.setdefault(fact_key, fact_value)
        if prior_value != fact_value:
            raise ValueError("persisted recommendation snapshot contains conflicting duplicate identity/date facts")
    return snapshot


def _load_snapshot(raw: object) -> dict[str, Any]:
    return _validate_snapshot(_load_persisted_object(raw, field="recommendation snapshot"))


def _source_ids(candidate: dict[str, Any]) -> list[str]:
    values = list(dict.fromkeys(candidate.get("source_ids", []) + candidate.get("ledger_source_ids", [])))
    if any(not isinstance(value, str) or not SOURCE_ID_RE.fullmatch(value) for value in values):
        raise ValueError("recommendation source IDs must be canonical stored source IDs")
    return values


def _persisted_source_ids(raw: object) -> list[str]:
    if not isinstance(raw, str):
        raise ValueError("persisted recommendation source IDs are invalid JSON")
    try:
        values = json.loads(raw, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("persisted recommendation source IDs are invalid JSON") from exc
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not SOURCE_ID_RE.fullmatch(value) for value in values
    ):
        raise ValueError("persisted recommendation source IDs have an invalid structure")
    return list(dict.fromkeys(values))


def _source_value(source: sqlite3.Row) -> float:
    try:
        value = float(source["value"])
    except (TypeError, ValueError) as exc:
        raise ValueError("source value is not numeric") from exc
    if not math.isfinite(value):
        raise ValueError("source value is not finite")
    return value


def _safe_return(numerator: object, denominator: object) -> float | None:
    """Return a finite relative return, or unavailable for malformed inputs."""
    try:
        result = safe_divide(float(numerator), float(denominator))
    except (TypeError, ValueError):
        return None
    if result is None:
        return None
    relative = result - 1
    return relative if math.isfinite(relative) else None


def _conflicting_outcome_source(
    connection: sqlite3.Connection,
    source: sqlite3.Row,
    *,
    evaluation_date: date,
) -> bool:
    return same_date_fact_conflict(
        connection, source, as_of=evaluation_date.isoformat(),
    )


def _typed_identifier_matches(
    connection: sqlite3.Connection,
    source: sqlite3.Row,
    *,
    expected_type: str | None,
    expected_value: str | None,
) -> bool:
    """Match exact typed identifiers, with only an explicit link exception."""
    if not expected_type or not expected_value:
        return False
    if (
        source["instrument_identifier_type"] == expected_type
        and source["instrument_identifier"] == expected_value
    ):
        return True
    return typed_identity_matches(
        connection,
        source["instrument_identifier_type"], source["instrument_identifier"],
        expected_type, expected_value,
    )


def _source_for_outcome(
    connection: sqlite3.Connection,
    source_id: str | None,
    *,
    field: str,
    cutoff: str,
    evaluation_date: str,
    identifier_type: str | None = None,
    identifier_value: str | None = None,
    currency: str | None = None,
    benchmark_identifier_type: str | None = None,
    benchmark_identifier_value: str | None = None,
) -> sqlite3.Row:
    if not source_id or not SOURCE_ID_RE.fullmatch(source_id):
        raise ValueError(f"missing cited {field} source evidence")
    source = connection.execute("SELECT * FROM source_records WHERE id = ?", (source_id,)).fetchone()
    if source is None or source["field"] != field:
        raise ValueError(f"source {source_id} is not cited {field} evidence")
    if source["observation_date"] is None:
        raise ValueError("outcome evidence must be after cutoff and no later than outcome date")
    cutoff_date = _date(cutoff, field="data_cutoff")
    evaluation_day = _date(evaluation_date, field="evaluation_date")
    observation_date = _date(source["observation_date"], field="observation_date")
    if not cutoff_date < observation_date <= evaluation_day:
        raise ValueError("outcome evidence must be after cutoff and no later than outcome date")
    try:
        retrieved_date = parse_retrieved_at(source["retrieved_at"]).date()
    except ValueError as exc:
        raise ValueError("invalid source retrieval date") from exc
    if retrieved_date < observation_date:
        raise ValueError("outcome evidence retrieval date cannot precede observation date")
    if retrieved_date < cutoff_date:
        raise ValueError("outcome evidence was retrieved before the recommendation cutoff")
    if retrieved_date > evaluation_day:
        raise ValueError("outcome evidence was not available by the outcome date")
    expected_type = identifier_type if field == "price" else benchmark_identifier_type
    expected_value = identifier_value if field == "price" else benchmark_identifier_value
    identifier_matches = _typed_identifier_matches(
        connection, source, expected_type=expected_type, expected_value=expected_value,
    )
    if not identifier_matches:
        raise ValueError(f"{field} source does not match the declared typed instrument")
    if currency is not None and source["currency"] != currency:
        raise ValueError("outcome source currency does not match the recommendation")
    if not source_fact_is_usable(source):
        raise ValueError("outcome evidence must be a usable fresh observation")
    if _conflicting_outcome_source(connection, source, evaluation_date=_date(evaluation_date, field="evaluation_date")):
        raise ValueError("outcome evidence conflicts with another same-date source")
    _source_value(source)
    return source


def record_recommendation(
    connection: sqlite3.Connection,
    candidate: dict[str, Any],
    *,
    data_cutoff: str,
    provider: str,
    template_version: str,
    snapshot: dict[str, Any] | None = None,
) -> int:
    """Store a generated, reproducible candidate snapshot after strict validation."""
    cutoff_date = _date(data_cutoff, field="data_cutoff")
    if cutoff_date > _utc_today():
        raise ValueError("data_cutoff cannot be in the future")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider is required")
    if not isinstance(template_version, str) or not template_version.strip():
        raise ValueError("template_version is required")
    if candidate.get("label") not in LABELS:
        raise ValueError("recommendation label is unsupported")
    generated_snapshot = calculate_snapshot(connection, as_of=data_cutoff)
    if snapshot is not None:
        if snapshot.get("as_of") != data_cutoff:
            raise ValueError("recommendation snapshot as_of must equal data_cutoff")
        _, supplied_hash = _canonical(snapshot)
        _, generated_hash = _canonical(generated_snapshot)
        if supplied_hash != generated_hash:
            raise ValueError("supplied snapshot is not the generated snapshot for this cutoff")
    snapshot_json, snapshot_hash = _canonical(generated_snapshot)
    identifier_type = candidate.get("identifier_type")
    identifier_value = candidate.get("identifier_value", candidate.get("instrument"))
    if not identifier_type or not identifier_value:
        raise ValueError("recommendation instrument must include a typed identifier")
    known = connection.execute(
        "SELECT currency, benchmark FROM instruments WHERE identifier_type = ? AND identifier_value = ?",
        (identifier_type, identifier_value),
    ).fetchone()
    if known is None:
        known = connection.execute(
            "SELECT currency, benchmark FROM watchlist WHERE identifier_type = ? AND identifier_value = ?",
            (identifier_type, identifier_value),
        ).fetchone()
    if known is None:
        raise ValueError("recommendation instrument does not exist in the local database")
    source_ids = _source_ids(candidate)
    conflicting_source_ids = fact_conflict_source_ids(connection, as_of=data_cutoff)
    if set(source_ids) & conflicting_source_ids:
        raise ValueError("recommendation cannot cite conflicting source facts")
    for source_id in source_ids:
        if connection.execute("SELECT 1 FROM source_records WHERE id = ?", (source_id,)).fetchone() is None:
            raise ValueError(f"recommendation cites nonexistent source {source_id}")
    generated_candidates = run_screens(connection, generated_snapshot, as_of=data_cutoff)
    matching = next((item for item in generated_candidates if item["identifier_type"] == identifier_type and item["identifier_value"] == identifier_value), None)
    if matching is None:
        raise ValueError("recommendation candidate is not present in the generated snapshot")
    if _canonical(candidate)[1] != _canonical(matching)[1]:
        raise ValueError("recommendation candidate is not the generated candidate")
    generated_source_ids = set(matching.get("source_ids", [])) | set(matching.get("ledger_source_ids", []))
    if set(source_ids) != generated_source_ids:
        raise ValueError("recommendation citations must exactly match the generated candidate")
    if not set(matching.get("ledger_source_ids", [])).issubset(source_ids):
        raise ValueError("recommendation is missing deterministic ledger provenance")
    benchmark_value = matching.get("benchmark_identifier_value")
    benchmark_type = matching.get("benchmark_identifier_type")
    freshness = matching.get("metrics", {}).get("price_status")
    if benchmark_value is not None and not benchmark_type:
        # A legacy scalar benchmark is retained for display but cannot be used
        # for a typed recommendation outcome.
        benchmark_type = None
    stored_currency = matching.get("currency", known["currency"])
    stored_freshness = matching.get("metrics", {}).get("price_status")
    contract_hash = _recommendation_contract_hash(
        data_cutoff=data_cutoff, provider=provider, template_version=template_version,
        source_ids=source_ids, instrument=matching["instrument"], label=matching["label"],
        metrics=matching["metrics"], reason=candidate["reason"], risk=candidate["risk_counter_evidence"],
        horizon=candidate["horizon"], invalidation=candidate["invalidation"],
        snapshot_json=snapshot_json, snapshot_hash=snapshot_hash,
        identifier_type=identifier_type, identifier_value=identifier_value,
        benchmark_identifier_type=benchmark_type, benchmark_identifier_value=benchmark_value,
        currency=stored_currency, freshness_status=stored_freshness,
        provider_contract=provider, template_contract=template_version,
    )
    cursor = connection.execute(
        """
        INSERT INTO recommendations(
            created_at, data_cutoff, provider, template_version, provider_contract, template_contract, source_ids, instrument,
            label, metrics_json, reason, risk, horizon, invalidation,
            snapshot_json, snapshot_hash, contract_hash, identifier_type, identifier_value,
            benchmark_identifier_type, benchmark_identifier_value, currency, freshness_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _now(), data_cutoff, provider, template_version, provider, template_version,
            json.dumps(source_ids, sort_keys=True, allow_nan=False),
            matching["instrument"], matching["label"],
            json.dumps(matching["metrics"], sort_keys=True, allow_nan=False),
            candidate["reason"], candidate["risk_counter_evidence"], candidate["horizon"], candidate["invalidation"],
            snapshot_json, snapshot_hash, contract_hash, identifier_type, identifier_value,
            benchmark_type, benchmark_value, stored_currency, stored_freshness,
        ),
    )
    recommendation_id = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO recommendation_integrity(
            recommendation_id, provider, provider_contract, template_version,
            template_contract, contract_hash
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (recommendation_id, provider, provider, template_version, template_version, contract_hash),
    )
    connection.commit()
    return recommendation_id


def evaluate_recommendation(
    connection: sqlite3.Connection,
    recommendation_id: int,
    *,
    evaluation_date: str,
    observed_price: float | None = None,
    benchmark_price: float | None = None,
    observed_price_source_id: str | None = None,
    benchmark_price_source_id: str | None = None,
) -> None:
    """Append an outcome only when later cited observations reproduce the scope."""
    evaluation = _date(evaluation_date, field="evaluation_date")
    row = connection.execute("SELECT * FROM recommendations WHERE id = ?", (recommendation_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown recommendation: {recommendation_id}")
    metrics = _load_metrics(row["metrics_json"])
    persisted_snapshot = _load_snapshot(row["snapshot_json"])
    integrity = connection.execute(
        "SELECT * FROM recommendation_integrity WHERE recommendation_id = ?",
        (recommendation_id,),
    ).fetchone()
    if integrity is None:
        raise ValueError("recommendation integrity evidence is missing")
    if any(
        row[column] != integrity[column]
        for column in ("provider", "provider_contract", "template_version", "template_contract", "contract_hash")
    ):
        raise ValueError("persisted recommendation provider/template integrity is invalid")
    if (
        not isinstance(row["provider_contract"], str)
        or not row["provider_contract"].strip()
        or not isinstance(row["template_contract"], str)
        or not row["template_contract"].strip()
        or row["provider"] != row["provider_contract"]
        or row["template_version"] != row["template_contract"]
    ):
        raise ValueError("persisted recommendation provider/template does not match its contract")
    snapshot_cutoff = _date(persisted_snapshot["as_of"], field="persisted snapshot cutoff")
    if row["data_cutoff"] != persisted_snapshot["as_of"]:
        raise ValueError("persisted recommendation snapshot cutoff does not match recommendation")
    canonical_snapshot_json, canonical_snapshot_hash = _canonical(persisted_snapshot)
    if row["snapshot_json"] != canonical_snapshot_json:
        raise ValueError("persisted recommendation snapshot is not canonical JSON")
    if not isinstance(row["snapshot_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["snapshot_hash"]):
        raise ValueError("recommendation snapshot hash is missing or invalid")
    if canonical_snapshot_hash != row["snapshot_hash"]:
        raise ValueError("recommendation snapshot hash does not match persisted snapshot")
    persisted_candidates = run_screens(connection, persisted_snapshot, as_of=row["data_cutoff"])
    matching = next(
        (
            candidate for candidate in persisted_candidates
            if candidate["identifier_type"] == row["identifier_type"]
            and candidate["identifier_value"] == row["identifier_value"]
        ),
        None,
    )
    if matching is None:
        raise ValueError("persisted recommendation candidate is absent from its snapshot")
    expected_source_ids = _source_ids(matching)
    persisted_source_ids = _persisted_source_ids(row["source_ids"])
    if persisted_source_ids != expected_source_ids:
        raise ValueError("persisted recommendation citations do not match its snapshot")
    snapshot_source_ids = {
        source["id"] for source in persisted_snapshot["sources"]
    }
    if not set(persisted_source_ids) <= snapshot_source_ids:
        raise ValueError("persisted recommendation cites a source absent from its snapshot")
    expected_benchmark_type = matching.get("benchmark_identifier_type")
    expected_benchmark_value = matching.get("benchmark_identifier_value")
    stored_currency = matching.get("currency")
    expected_contract_hash = _recommendation_contract_hash(
        data_cutoff=row["data_cutoff"], provider=row["provider"],
        template_version=row["template_version"], source_ids=expected_source_ids,
        instrument=matching["instrument"], label=matching["label"], metrics=matching["metrics"],
        reason=matching["reason"], risk=matching["risk_counter_evidence"],
        horizon=matching["horizon"], invalidation=matching["invalidation"],
        snapshot_json=canonical_snapshot_json, snapshot_hash=row["snapshot_hash"],
        identifier_type=matching["identifier_type"], identifier_value=matching["identifier_value"],
        benchmark_identifier_type=expected_benchmark_type,
        benchmark_identifier_value=expected_benchmark_value, currency=stored_currency,
        freshness_status=matching["metrics"]["price_status"],
        provider_contract=integrity["provider_contract"],
        template_contract=integrity["template_contract"],
    )
    if not isinstance(row["contract_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["contract_hash"]):
        raise ValueError("recommendation contract hash is missing or invalid")
    if row["contract_hash"] != expected_contract_hash:
        raise ValueError("recommendation contract does not match its persisted snapshot")
    if row["instrument"] != matching["instrument"] or row["label"] != matching["label"]:
        raise ValueError("persisted recommendation candidate identity does not match its snapshot")
    if metrics != matching["metrics"]:
        raise ValueError("persisted recommendation metrics do not match its snapshot")
    if any(
        row[column] != matching[value]
        for column, value in (
            ("reason", "reason"), ("risk", "risk_counter_evidence"),
            ("horizon", "horizon"), ("invalidation", "invalidation"),
            ("identifier_type", "identifier_type"), ("identifier_value", "identifier_value"),
            ("benchmark_identifier_type", "benchmark_identifier_type"),
            ("benchmark_identifier_value", "benchmark_identifier_value"),
            ("currency", "currency"),
        )
    ) or row["freshness_status"] != matching["metrics"]["price_status"]:
        raise ValueError("persisted recommendation contract fields do not match its snapshot")
    if not isinstance(row["provider"], str) or not row["provider"].strip() or not isinstance(row["template_version"], str) or not row["template_version"].strip():
        raise ValueError("persisted recommendation provider/template contract is invalid")
    generated_snapshot = calculate_snapshot(connection, as_of=row["data_cutoff"])
    _, generated_snapshot_hash = _canonical(generated_snapshot)
    if generated_snapshot_hash != row["snapshot_hash"]:
        raise ValueError("recommendation snapshot is not the generated snapshot for its cutoff")
    if set(persisted_source_ids) & fact_conflict_source_ids(connection, as_of=row["data_cutoff"]):
        raise ValueError("persisted recommendation cites conflicting source facts")
    generated_candidates = run_screens(connection, generated_snapshot, as_of=row["data_cutoff"])
    generated_matching = next(
        (
            candidate for candidate in generated_candidates
            if candidate["identifier_type"] == row["identifier_type"]
            and candidate["identifier_value"] == row["identifier_value"]
        ),
        None,
    )
    if generated_matching is None or _canonical(generated_matching)[1] != _canonical(matching)[1]:
        raise ValueError("persisted recommendation candidate is not the generated candidate")
    cutoff = _date(row["data_cutoff"], field="data_cutoff")
    if cutoff != snapshot_cutoff or evaluation <= cutoff:
        raise ValueError("evaluation date must be after recommendation cutoff")
    observed = _source_for_outcome(
        connection, observed_price_source_id, field="price", cutoff=row["data_cutoff"], evaluation_date=evaluation_date,
        identifier_type=row["identifier_type"], identifier_value=row["identifier_value"], currency=row["currency"],
    )
    benchmark = _source_for_outcome(
        connection, benchmark_price_source_id, field="benchmark_price", cutoff=row["data_cutoff"], evaluation_date=evaluation_date,
        identifier_type=row["benchmark_identifier_type"], identifier_value=row["benchmark_identifier_value"],
        benchmark_identifier_type=row["benchmark_identifier_type"], benchmark_identifier_value=row["benchmark_identifier_value"],
        currency=row["currency"],
    )
    cited_price = float(observed["value"])
    cited_benchmark = float(benchmark["value"])
    if observed_price is not None and (not math.isfinite(observed_price) or observed_price != cited_price):
        raise ValueError("caller price does not match cited source observation")
    if benchmark_price is not None and (not math.isfinite(benchmark_price) or benchmark_price != cited_benchmark):
        raise ValueError("caller benchmark does not match cited source observation")
    original_price = metrics.get("current_price")
    original_benchmark = metrics.get("benchmark_price")
    observed_return = _safe_return(cited_price, original_price) if original_price else None
    benchmark_return = _safe_return(cited_benchmark, original_benchmark) if original_benchmark else None
    outcome_snapshot = {
        "recommendation_snapshot_hash": row["snapshot_hash"], "evaluation_date": evaluation_date,
        "observed_price": cited_price, "benchmark_price": cited_benchmark,
        "original_price": original_price, "original_benchmark_price": original_benchmark,
        "observed_source": dict(observed), "benchmark_source": dict(benchmark),
    }
    connection.execute(
        """
        INSERT INTO recommendation_outcomes(
            recommendation_id, evaluation_date, observed_price, benchmark_price,
            observed_return, benchmark_return, snapshot_json,
            observed_price_source_id, benchmark_price_source_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            recommendation_id, evaluation_date, cited_price, cited_benchmark, observed_return, benchmark_return,
            _canonical(outcome_snapshot)[0], observed["id"], benchmark["id"],
        ),
    )
    connection.commit()
