"""Forward return labels with explicit, non-overlapping interval metadata."""

from __future__ import annotations

from datetime import date, timedelta
import math
from numbers import Real
from typing import Any, Iterable, Mapping

from .historical_market_data import is_usable_market_bar

SUPPORTED_RETURN_BASES = frozenset({"price_return", "total_return"})
TARGET_HORIZONS = ("1m", "3m", "6m", "12m")
TARGET_ARITHMETIC_TOLERANCE = 1e-12
TARGET_AVAILABILITY_STATUSES = frozenset({
    "available_at_endpoint", "available_after_endpoint",
    "unavailable_endpoint_not_observable", "unavailable_no_next_monthly_decision",
})
TARGET_HORIZON_DEFINITION = (
    "1m uses the next monthly decision date as a disjoint forward interval; "
    "3m uses 90 calendar days; 6m uses 180 calendar days; 12m uses 365 calendar days."
)
TARGET_HORIZON_DAYS = {"3m": 90, "6m": 180, "12m": 365}


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _price(bar: Any, basis: str) -> float | None:
    if not is_usable_market_bar(bar):
        return None
    value = getattr(bar, "close", None) if basis == "price_return" else getattr(bar, "adjusted_close", None)
    if value is None and basis == "total_return":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _horizon_return(
    bars: Iterable[Any], decision: date, endpoint: date, *, basis: str,
) -> tuple[float | None, str | None]:
    ordered = sorted((bar for bar in bars if is_usable_market_bar(bar) and _as_date(bar.observation_date) <= decision), key=lambda b: b.observation_date)
    start = ordered[-1] if ordered else None
    future = sorted((bar for bar in bars if is_usable_market_bar(bar) and _as_date(bar.observation_date) >= endpoint), key=lambda b: b.observation_date)
    finish = future[0] if future else None
    first, last = (_price(start, basis) if start else None), (_price(finish, basis) if finish else None)
    if first is None or last is None:
        return None, finish.observation_date if finish else None
    return last / first - 1.0, finish.observation_date


def _target_number(value: Any, *, path: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{path} must be a finite number or null")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{path} must be a finite number or null")
    return number


def _target_date(value: Any, *, path: str, nullable: bool = False) -> date | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path} must be an ISO date" + (" or null" if nullable else ""))
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{path} must be an ISO date" + (" or null" if nullable else "")) from exc


def validate_target_contract(
    row: Any, *, expected_basis: str = "price_return", strict: bool = False,
) -> None:
    """Validate serialized target arithmetic and the metadata that governs it.

    Legacy in-memory fixtures may omit an entire return component for a horizon.
    A horizon with a complete return triplet, or with typed target metadata, is
    validated as a serialized contract; missing values in that contract remain
    explicit unavailable values rather than being inferred.
    """
    targets = getattr(row, "targets", None)
    metadata = getattr(row, "target_metadata", None)
    if not isinstance(targets, Mapping):
        raise ValueError("target fields are required")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ValueError("target metadata must be an object or null")
    decision = _target_date(getattr(row, "decision_date", None), path="target decision_date")
    if expected_basis not in SUPPORTED_RETURN_BASES:
        raise ValueError("target return basis is unsupported")
    top_basis = targets.get("target_return_basis")
    if top_basis is not None and (not isinstance(top_basis, str) or top_basis != expected_basis):
        raise ValueError("mixed return bases are not permitted")

    for horizon in TARGET_HORIZONS:
        prefix = f"target_{horizon}_"
        keys = {
            key for source in (targets, metadata or {})
            for key in source if isinstance(key, str) and key.startswith(prefix)
        }
        if not keys:
            continue
        return_keys = tuple(f"{prefix}{suffix}" for suffix in ("return", "benchmark_return", "excess_return"))
        complete_triplet = all(key in targets for key in return_keys)
        typed_metadata = any(key in (metadata or {}) for key in keys)
        enforce_complete = strict or complete_triplet or typed_metadata
        source: dict[str, Any] = dict(targets)
        for key in keys:
            if key in (metadata or {}):
                if key in targets and targets[key] != metadata[key]:
                    raise ValueError(f"{key} differs between target and target metadata")
                source[key] = metadata[key]

        basis_key = f"{prefix}return_basis"
        basis = source.get(basis_key)
        if basis is not None and (not isinstance(basis, str) or basis != expected_basis):
            raise ValueError("mixed return bases are not permitted")
        interval_start_key = f"{prefix}interval_start"
        interval_end_key = f"{prefix}interval_end"
        endpoint_key = f"{prefix}forward_endpoint"
        semantics_key = f"{prefix}interval_semantics"
        interval_start = _target_date(source.get(interval_start_key), path=interval_start_key) if interval_start_key in source else decision
        interval_end = _target_date(source.get(interval_end_key), path=interval_end_key) if interval_end_key in source else None
        endpoint = _target_date(source.get(endpoint_key), path=endpoint_key) if endpoint_key in source else None
        if interval_start is not None and decision is not None and interval_start != decision:
            raise ValueError(f"{interval_start_key} must match decision_date")
        if interval_end is not None and endpoint is not None and interval_end != endpoint:
            raise ValueError(f"{interval_end_key} must match {endpoint_key}")
        if semantics_key in source and source[semantics_key] != "[start,end)":
            raise ValueError(f"{semantics_key} is invalid")
        if endpoint is not None and decision is not None and endpoint <= decision:
            raise ValueError(f"{endpoint_key} must follow decision_date")
        if horizon in TARGET_HORIZON_DAYS and endpoint is not None and decision is not None:
            expected_endpoint = decision + timedelta(days=TARGET_HORIZON_DAYS[horizon])
            if endpoint != expected_endpoint:
                raise ValueError(
                    f"{prefix} endpoint does not match the documented {TARGET_HORIZON_DAYS[horizon]}-day horizon"
                )
        status_key = f"{prefix}label_availability"
        status = source.get(status_key)
        if status is not None and (not isinstance(status, str) or status not in TARGET_AVAILABILITY_STATUSES):
            raise ValueError(f"{status_key} is invalid")
        endpoint_available_key = f"{prefix}endpoint_available"
        endpoint_available = source.get(endpoint_available_key)
        if endpoint_available is not None and not isinstance(endpoint_available, bool):
            raise ValueError(f"{endpoint_available_key} must be boolean")
        if endpoint_available is not None and status is not None and endpoint_available != (status == "available_at_endpoint"):
            raise ValueError(f"{endpoint_available_key} is inconsistent with {status_key}")
        if enforce_complete and endpoint_available is not None and status is None:
            raise ValueError(f"{status_key} is required with {endpoint_available_key}")

        observations: list[date | None] = []
        for suffix in ("asset_observation_date", "benchmark_observation_date"):
            key = f"{prefix}{suffix}"
            value = _target_date(source.get(key), path=key, nullable=True) if key in source else None
            observations.append(value)
            if value is not None and endpoint is not None and status == "available_at_endpoint" and value > endpoint:
                raise ValueError(f"{key} is after the target endpoint")
        values = [_target_number(source.get(key), path=key) for key in return_keys]
        asset, benchmark, excess = values
        total_key = f"{prefix}total_return"
        total = _target_number(source.get(total_key), path=total_key) if total_key in source else None
        if total_key in source and ((total is None) != (asset is None) or (total is not None and asset is not None and not math.isclose(total, asset, rel_tol=0.0, abs_tol=TARGET_ARITHMETIC_TOLERANCE))):
            raise ValueError(f"{total_key} does not match asset return")
        if all(value is not None for value in (asset, benchmark, excess)) and not math.isclose(
            excess, asset - benchmark, rel_tol=0.0, abs_tol=TARGET_ARITHMETIC_TOLERANCE,
        ):
            raise ValueError(f"{prefix}excess_return does not equal asset minus benchmark")
        if enforce_complete and status == "available_after_endpoint":
            late_asset = observations[0] is not None and endpoint is not None and observations[0] > endpoint
            late_benchmark = observations[1] is not None and endpoint is not None and observations[1] > endpoint
            if not late_asset and not late_benchmark:
                raise ValueError(f"{status_key} has no after-endpoint observation")
            if (late_asset and asset is not None) or (late_benchmark and benchmark is not None) or excess is not None:
                raise ValueError(f"{status_key} is inconsistent with its returns")
        if enforce_complete and status == "available_at_endpoint":
            if any(value is None for value in (asset, benchmark, excess)) or any(value is None for value in observations):
                raise ValueError(f"{horizon} available target is incomplete")
        if enforce_complete and status in {"available_after_endpoint", "unavailable_endpoint_not_observable", "unavailable_no_next_monthly_decision"}:
            if excess is not None and status != "available_after_endpoint":
                raise ValueError(f"{horizon} unavailable target has an excess return")
            if status != "available_after_endpoint" and all(value is not None for value in (asset, benchmark, excess)):
                raise ValueError(f"{horizon} unavailable target has a complete return triplet")


def _target_metadata_source(row: Any, horizon: str) -> Mapping[str, Any]:
    """Return typed target metadata, falling back to serialized target fields."""
    targets = getattr(row, "targets", None)
    if not isinstance(targets, Mapping):
        raise ValueError("target fields are required")
    metadata = getattr(row, "target_metadata", None)
    prefix = f"target_{horizon}_"
    metadata_fields = (
        f"{prefix}forward_endpoint",
        f"{prefix}label_availability",
        f"{prefix}endpoint_available",
        f"{prefix}asset_observation_date",
        f"{prefix}benchmark_observation_date",
    )
    if isinstance(metadata, Mapping) and any(field in metadata for field in metadata_fields):
        return metadata
    return targets


def target_label_availability(row: Any, horizon: str = "3m") -> str:
    """Resolve a target label's observation status from typed or serialized fields."""
    source = _target_metadata_source(row, horizon)
    prefix = f"target_{horizon}_"
    endpoint_raw = source.get(f"{prefix}forward_endpoint")
    endpoint = _as_date(endpoint_raw) if endpoint_raw is not None else None
    observation_dates = [
        source.get(f"{prefix}asset_observation_date"),
        source.get(f"{prefix}benchmark_observation_date"),
    ]
    if endpoint is not None and any(
        observation is not None and _as_date(observation) > endpoint
        for observation in observation_dates
    ):
        return "available_after_endpoint"
    endpoint_available = source.get(f"{prefix}endpoint_available")
    if endpoint_available is False:
        return "unavailable_endpoint_not_observable"
    status = source.get(f"{prefix}label_availability")
    if isinstance(status, str) and status:
        return status
    if endpoint_available is True:
        return "available_at_endpoint"
    if endpoint is not None and all(observation is not None for observation in observation_dates):
        return "available_at_endpoint"
    return "unavailable_endpoint_not_observable"


def target_label_available_by(
    row: Any,
    observable_by: date | str,
    horizon: str = "3m",
) -> bool:
    """Return whether a target is valid and observable by the supplied date."""
    source = _target_metadata_source(row, horizon)
    endpoint_key = f"target_{horizon}_forward_endpoint"
    endpoint_raw = source.get(endpoint_key)
    if endpoint_raw is None:
        raise ValueError(f"target_{horizon} forward endpoint is required")
    endpoint = _as_date(endpoint_raw)
    if endpoint > _as_date(observable_by):
        return False
    return target_label_availability(row, horizon) == "available_at_endpoint"


def calculate_forward_targets(
    bars_by_ticker: Mapping[str, Iterable[Any]],
    benchmark_bars: Iterable[Any],
    decision_date: date | str,
    *,
    next_decision_date: date | str | None = None,
    return_basis: str = "price_return",
    monthly_interval: bool = False,
) -> dict[str, Any]:
    """Calculate asset, benchmark, and excess labels.

    The legacy field names containing ``total_return`` are retained for API
    compatibility; their declared basis is always carried alongside them.
    """
    if return_basis not in SUPPORTED_RETURN_BASES:
        raise ValueError("return_basis must be price_return or total_return")
    decision = _as_date(decision_date)
    next_date = _as_date(next_decision_date) if next_decision_date is not None else None
    if next_date is not None and next_date <= decision:
        raise ValueError("next monthly decision date must follow decision date")
    benchmark = list(benchmark_bars)
    out: dict[str, Any] = {"decision_date": decision.isoformat(), "target_return_basis": return_basis}
    asset_bars = next(iter(bars_by_ticker.values()), ())
    for label, days in (("1m", 30), ("3m", 90), ("6m", 180), ("12m", 365)):
        endpoint = next_date if label == "1m" and next_date is not None else decision + timedelta(days=days)
        if label == "1m" and monthly_interval and next_date is None:
            asset_value = benchmark_value = None
            asset_observation = benchmark_observation = None
            available = "unavailable_no_next_monthly_decision"
        else:
            asset_value, asset_observation = _horizon_return(asset_bars, decision, endpoint, basis=return_basis)
            benchmark_value, benchmark_observation = _horizon_return(benchmark, decision, endpoint, basis=return_basis)
            late_observation = any(
                observation is not None and _as_date(observation) > endpoint
                for observation in (asset_observation, benchmark_observation)
            )
            if late_observation:
                available = "available_after_endpoint"
            elif asset_value is not None and benchmark_value is not None:
                available = "available_at_endpoint"
            else:
                available = "unavailable_endpoint_not_observable"
            if asset_observation is not None and _as_date(asset_observation) > endpoint:
                asset_value = None
            if benchmark_observation is not None and _as_date(benchmark_observation) > endpoint:
                benchmark_value = None
        out[f"target_{label}_interval_start"] = decision.isoformat()
        out[f"target_{label}_interval_end"] = endpoint.isoformat()
        out[f"target_{label}_interval_semantics"] = "[start,end)"
        out[f"target_{label}_forward_endpoint"] = endpoint.isoformat()
        out[f"target_{label}_return_basis"] = return_basis
        out[f"target_{label}_endpoint_available"] = available == "available_at_endpoint"
        out[f"target_{label}_label_availability"] = available
        out[f"target_{label}_asset_observation_date"] = asset_observation
        out[f"target_{label}_benchmark_observation_date"] = benchmark_observation
        out[f"target_{label}_return"] = asset_value
        out[f"target_{label}_benchmark_return"] = benchmark_value
        out[f"target_{label}_total_return"] = out.get(f"target_{label}_return")
        out[f"target_{label}_excess_return"] = (
            out.get(f"target_{label}_return") - benchmark_value
            if out.get(f"target_{label}_return") is not None and benchmark_value is not None else None
        )
    out["target_1m_interval_definition"] = "next_monthly_decision_date" if next_date else "fixed_30_calendar_days"
    return out
