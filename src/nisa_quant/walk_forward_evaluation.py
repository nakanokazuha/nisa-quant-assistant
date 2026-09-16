"""Purged walk-forward evaluation with explicit return/cost conventions."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from .ranking_model import MISSING_FEATURE_POLICY, MODEL_VERSION, SpecializedRankingModel, _validate_training_window, factor_score
from .return_targets import target_label_available_by, validate_target_contract
from .scenario_analysis import build_scenarios

PORTFOLIO_RETURN_BASIS = "price_return"
PORTFOLIO_INTERVAL_CONVENTION = "non-overlapping [start,end) monthly interval ending at the next monthly decision date"
TURNOVER_CONVENTION = "symmetric difference of current and prior holdings divided by current selected count"
DEFAULT_FACTOR_TOP_K = 5
INSUFFICIENT_DATA_REASON = "zero validation periods or zero eligible selections"
BACKTEST_SCHEMA_VERSION = 1
BACKTEST_METRIC_FIELDS = frozenset({
    "benchmark_cumulative_return", "transaction_cost_bps", "turnover_convention",
    "portfolio_return_basis", "benchmark_return_basis", "portfolio_interval_convention",
    "benchmark_relative_return", "evidence_status", "performance_claims_suppressed",
    "performance_claims_unavailable", "benchmark_relative_claims_suppressed",
    "alpha_claim_suppressed", "availability_status", "insufficient_data_reason",
})
BACKTEST_PERIOD_FIELDS = frozenset({
    "decision_date", "benchmark_return", "factor_baseline_requested",
    "factor_baseline_selected", "factor_baseline_tickers", "turnover_convention",
    "factor_baseline_return", "factor_baseline_turnover",
})
BACKTEST_AVAILABILITY_STATUSES = frozenset({
    "available", "available_descriptive", "unavailable_insufficient_data",
})
SCENARIO_FIELDS = frozenset({"confidence", "bear", "base", "bull", "interpretation"})
_TOP_K_METRIC_PATTERN = re.compile(r"^top_(\d+)_(cumulative_return|benchmark_relative_return)$")


def _validate_typed_target_contract(row: Any) -> None:
    from .training_dataset import PanelRow
    if isinstance(row, PanelRow):
        validate_target_contract(row)


@dataclass(frozen=True, slots=True)
class BacktestResult:
    periods: list[dict[str, Any]]
    metrics: dict[str, Any]
    return_basis: str = "price_return"
    dataset_id: str = ""
    history_snapshot_id: str = ""
    benchmark_ticker: str = "^GSPC"
    feature_schema: tuple[str, ...] = ()
    model_version: str = MODEL_VERSION
    target_horizon: str = "3m"
    training_cutoff: str | None = None
    training_window: int | None = None
    validation_dates: tuple[str, ...] = ()
    transaction_cost_bps: float = 10.0
    model_artifact_id: str | None = None
    scenarios: dict[str, Any] | None = None
    schema_version: int = BACKTEST_SCHEMA_VERSION
    backtest_id: str = ""
    missing_feature_policy: str | None = None


def _backtest_payload(result: BacktestResult) -> dict[str, Any]:
    payload = asdict(result)
    payload.pop("backtest_id", None)
    return payload


def _backtest_hash(result: BacktestResult) -> str:
    return hashlib.sha256(json.dumps(_backtest_payload(result), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _with_backtest_identity(result: BacktestResult) -> BacktestResult:
    digest = _backtest_hash(result)
    return BacktestResult(**{**asdict(result), "backtest_id": f"backtest-{digest[:20]}"})


def _validate_backtest(result: BacktestResult) -> None:
    if isinstance(result.schema_version, bool) or not isinstance(result.schema_version, int) or result.schema_version != BACKTEST_SCHEMA_VERSION or result.return_basis != "price_return":
        raise ValueError("backtest schema or return basis is unsupported")
    if not all(isinstance(value, str) and value for value in (result.dataset_id, result.history_snapshot_id, result.benchmark_ticker, result.model_version, result.target_horizon, result.backtest_id)):
        raise ValueError("backtest binding fields are invalid")
    if not isinstance(result.feature_schema, tuple) or not result.feature_schema or any(
        not isinstance(value, str) or not value for value in result.feature_schema
    ) or result.target_horizon not in {"3m", "6m", "12m"}:
        raise ValueError("backtest target or feature schema is invalid")
    if any(not isinstance(value, str) or not value for value in result.validation_dates) or len(set(result.validation_dates)) != len(result.validation_dates):
        raise ValueError("backtest validation dates are invalid or duplicated")
    for value in result.validation_dates:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("backtest validation dates are invalid or duplicated") from exc
    if result.training_cutoff is not None:
        if not isinstance(result.training_cutoff, str) or not result.training_cutoff:
            raise ValueError("backtest training cutoff is invalid")
        try:
            date.fromisoformat(result.training_cutoff)
        except ValueError as exc:
            raise ValueError("backtest training cutoff is invalid") from exc
    _validate_training_window(result.training_window, label="backtest training window")
    if result.model_artifact_id is not None and (
        not isinstance(result.model_artifact_id, str) or not result.model_artifact_id
    ):
        raise ValueError("backtest model artifact ID is invalid")
    if result.missing_feature_policy is not None and result.missing_feature_policy != MISSING_FEATURE_POLICY:
        raise ValueError("backtest missing-feature policy is invalid")
    if isinstance(result.transaction_cost_bps, bool) or not isinstance(result.transaction_cost_bps, (int, float)) or not math.isfinite(float(result.transaction_cost_bps)) or result.transaction_cost_bps < 0:
        raise ValueError("backtest transaction cost is invalid")
    if not isinstance(result.periods, list):
        raise ValueError("backtest periods must be a list")
    if not isinstance(result.metrics, dict):
        raise ValueError("backtest metrics must be an object")
    if result.scenarios is not None and not isinstance(result.scenarios, dict):
        raise ValueError("backtest scenarios must be an object or null")
    if result.scenarios is not None:
        if set(result.scenarios) != SCENARIO_FIELDS:
            raise ValueError("backtest scenarios have an invalid field set")
        if result.scenarios["confidence"] not in {"low", "medium", "high"} or not isinstance(result.scenarios["interpretation"], str) or not result.scenarios["interpretation"]:
            raise ValueError("backtest scenarios have invalid descriptive fields")

    top_ks: set[int] = set()
    for key in result.metrics:
        if not isinstance(key, str):
            raise ValueError("backtest metrics have an invalid field set")
        match = _TOP_K_METRIC_PATTERN.fullmatch(key)
        if match:
            raw_k = match.group(1)
            if str(int(raw_k)) != raw_k or int(raw_k) <= 0:
                raise ValueError("backtest metrics have an invalid top-k field")
            top_ks.add(int(raw_k))
        elif key not in BACKTEST_METRIC_FIELDS:
            raise ValueError(f"backtest metrics have an unexpected field: {key}")
    if not top_ks:
        raise ValueError("backtest metrics must include top-k cumulative fields")
    expected_metric_fields = set(BACKTEST_METRIC_FIELDS) - {"insufficient_data_reason"}
    expected_metric_fields.update(
        f"top_{k}_{suffix}" for k in top_ks for suffix in ("cumulative_return", "benchmark_relative_return")
    )
    if "insufficient_data_reason" in result.metrics:
        expected_metric_fields.add("insufficient_data_reason")
    if set(result.metrics) != expected_metric_fields:
        raise ValueError("backtest metrics have an invalid field set")

    def finite_number(value: Any, *, path: str, nullable: bool = False) -> None:
        if value is None and nullable:
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"backtest {path} must be a finite number")

    if result.scenarios is not None:
        for key in ("bear", "base", "bull"):
            finite_number(result.scenarios[key], path=f"scenarios.{key}", nullable=True)

    for key, value in result.metrics.items():
        if key in {"turnover_convention", "portfolio_return_basis", "benchmark_return_basis", "portfolio_interval_convention", "evidence_status", "availability_status"}:
            if not isinstance(value, str) or not value:
                raise ValueError(f"backtest metric {key} has an invalid type")
        elif key in {"performance_claims_suppressed", "performance_claims_unavailable", "benchmark_relative_claims_suppressed", "alpha_claim_suppressed"}:
            if not isinstance(value, bool):
                raise ValueError(f"backtest metric {key} has an invalid type")
        elif key == "insufficient_data_reason":
            if not isinstance(value, str) or not value:
                raise ValueError("backtest insufficient-data reason is invalid")
        elif key == "transaction_cost_bps":
            finite_number(value, path=f"metrics.{key}")
            if float(value) < 0:
                raise ValueError("backtest transaction cost is invalid")
        else:
            finite_number(value, path=f"metrics.{key}", nullable=True)
    if result.metrics["turnover_convention"] != TURNOVER_CONVENTION:
        raise ValueError("backtest metric turnover convention is invalid")
    if result.metrics["portfolio_return_basis"] != PORTFOLIO_RETURN_BASIS or result.metrics["benchmark_return_basis"] != PORTFOLIO_RETURN_BASIS:
        raise ValueError("backtest metric return basis is invalid")
    if result.metrics["portfolio_interval_convention"] != PORTFOLIO_INTERVAL_CONVENTION:
        raise ValueError("backtest metric interval convention is invalid")
    if result.metrics["evidence_status"] not in {"point_in_time_membership_evidence", "descriptive_survivor_selected_evidence"}:
        raise ValueError("backtest metric evidence status is invalid")
    availability = result.metrics["availability_status"]
    if availability not in BACKTEST_AVAILABILITY_STATUSES:
        raise ValueError("backtest metric availability status is invalid")
    if availability == "unavailable_insufficient_data":
        reason = result.metrics.get("insufficient_data_reason")
        if not isinstance(reason, str) or not reason:
            raise ValueError("backtest insufficient-data reason is required")
    elif "insufficient_data_reason" in result.metrics:
        raise ValueError("backtest insufficient-data reason is only valid for insufficient data")

    for index, period in enumerate(result.periods):
        if not isinstance(period, dict):
            raise ValueError(f"backtest period {index} must be an object")
        expected_period_fields = set(BACKTEST_PERIOD_FIELDS)
        expected_period_fields.update(
            f"top_{k}_{suffix}" for k in top_ks for suffix in ("selected", "return", "turnover")
        )
        if set(period) != expected_period_fields:
            raise ValueError(f"backtest period {index} has an invalid field set")
        decision_date = period["decision_date"]
        if not isinstance(decision_date, str) or not decision_date:
            raise ValueError(f"backtest period {index} decision date is invalid")
        try:
            date.fromisoformat(decision_date)
        except ValueError as exc:
            raise ValueError(f"backtest period {index} decision date is invalid") from exc
        finite_number(period["benchmark_return"], path=f"periods[{index}].benchmark_return", nullable=True)
        requested = period["factor_baseline_requested"]
        selected = period["factor_baseline_selected"]
        if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
            raise ValueError(f"backtest period {index} factor baseline requested is invalid")
        if isinstance(selected, bool) or not isinstance(selected, int) or not 0 <= selected <= requested:
            raise ValueError(f"backtest period {index} factor baseline selected is invalid")
        tickers = period["factor_baseline_tickers"]
        if not isinstance(tickers, list) or len(tickers) != selected or len(set(tickers)) != len(tickers) or any(
            not isinstance(ticker, str) or not ticker for ticker in tickers
        ):
            raise ValueError(f"backtest period {index} factor baseline tickers are invalid")
        if period["turnover_convention"] != TURNOVER_CONVENTION:
            raise ValueError(f"backtest period {index} turnover convention is invalid")
        finite_number(period["factor_baseline_return"], path=f"periods[{index}].factor_baseline_return", nullable=True)
        finite_number(period["factor_baseline_turnover"], path=f"periods[{index}].factor_baseline_turnover")
        if float(period["factor_baseline_turnover"]) < 0:
            raise ValueError(f"backtest period {index} factor baseline turnover is invalid")
        for k in top_ks:
            selected_count = period[f"top_{k}_selected"]
            if isinstance(selected_count, bool) or not isinstance(selected_count, int) or not 0 <= selected_count <= k:
                raise ValueError(f"backtest period {index} top_{k}_selected is invalid")
            finite_number(period[f"top_{k}_return"], path=f"periods[{index}].top_{k}_return", nullable=True)
            finite_number(period[f"top_{k}_turnover"], path=f"periods[{index}].top_{k}_turnover")
            if float(period[f"top_{k}_turnover"]) < 0:
                raise ValueError(f"backtest period {index} top_{k}_turnover is invalid")
    _validate_recomputed_metrics(result, top_ks)
    _validate_status_from_periods(result, top_ks)
    _validate_period_turnovers(result, top_ks)


def _endpoint(row: Any, horizon: str = "3m") -> date:
    raw = row.targets.get(f"target_{horizon}_forward_endpoint")
    if not isinstance(raw, str):
        raise ValueError(f"target_{horizon} forward endpoint is required")
    interval_end = row.targets.get(f"target_{horizon}_interval_end")
    if interval_end is not None and interval_end != raw:
        raise ValueError(f"target_{horizon} interval end does not match its forward endpoint")
    return date.fromisoformat(raw)


def _validate_monthly_target_contract(row: Any, target_horizon: str = "3m") -> None:
    targets = row.targets
    decision_date = row.decision_date
    if targets.get("target_1m_interval_start") != decision_date:
        raise ValueError("target_1m interval start must match decision date")
    endpoint = targets.get("target_1m_forward_endpoint")
    if not isinstance(endpoint, str) or targets.get("target_1m_interval_end") != endpoint:
        raise ValueError("target_1m interval endpoint must be explicit and consistent")
    if targets.get("target_1m_interval_semantics") != "[start,end)":
        raise ValueError("target_1m interval semantics must be [start,end)")
    if date.fromisoformat(endpoint) <= date.fromisoformat(decision_date):
        raise ValueError("target_1m interval endpoint must follow decision date")
    _endpoint(row, target_horizon)
    metadata = getattr(row, "target_metadata", None)
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ValueError("target metadata must be an object")
    for source in (targets, metadata or {}):
        for key, value in source.items():
            if key.endswith("_return_basis") and value != "price_return":
                raise ValueError("mixed return bases are not permitted")


def _validate_evaluation_periods(rows: Sequence[Any]) -> None:
    by_ticker: dict[str, list[tuple[date, date]]] = {}
    for row in rows:
        start = date.fromisoformat(row.targets["target_1m_interval_start"])
        end = date.fromisoformat(row.targets["target_1m_interval_end"])
        if end <= start:
            raise ValueError("evaluation period must end after it starts")
        by_ticker.setdefault(row.ticker, []).append((start, end))
    for ticker, periods in by_ticker.items():
        periods.sort()
        for previous, current in zip(periods, periods[1:]):
            if current[0] < previous[1]:
                raise ValueError(f"evaluation periods overlap for {ticker}")


def _validate_transaction_cost_bps(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("transaction cost must be finite and non-negative")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("transaction cost must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError("transaction cost must be finite and non-negative")
    return number


def walk_forward_splits(
    rows: Sequence[Any], *, validation_dates: Sequence[date | str], training_window: int | None = None,
    target_horizon: str = "3m",
):
    if target_horizon not in {"3m", "6m", "12m"}:
        raise ValueError("target horizon is unsupported")
    raw_dates = [value if isinstance(value, date) else date.fromisoformat(value) for value in validation_dates]
    if len(set(raw_dates)) != len(raw_dates):
        raise ValueError("duplicate validation dates are not permitted")
    if raw_dates != sorted(raw_dates):
        raise ValueError("validation dates must be strictly increasing")
    if training_window is not None and (isinstance(training_window, bool) or not isinstance(training_window, int) or training_window <= 0):
        raise ValueError("training window must be a positive integer or null")
    result = []
    for validation in raw_dates:
        candidates = [
            x for x in rows
            if date.fromisoformat(x.decision_date) < validation
            and target_label_available_by(x, validation, target_horizon)
        ]
        if training_window:
            candidates = candidates[-training_window:]
        test = [x for x in rows if date.fromisoformat(x.decision_date) == validation]
        result.append((candidates, test, validation))
    return result


def _metric(values: Sequence[Any]) -> float | None:
    finite = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not finite:
        return None
    cumulative = math.prod(1 + value for value in finite) - 1
    return cumulative if math.isfinite(cumulative) else None


def _validate_recomputed_metrics(result: BacktestResult, top_ks: set[int]) -> None:
    """Bind emitted numeric claims to validated serialized period rows."""
    expected: dict[str, Any] = {
        "benchmark_cumulative_return": _metric(
            [period["benchmark_return"] for period in result.periods]
        ),
        "transaction_cost_bps": float(result.transaction_cost_bps),
    }
    for k in top_ks:
        portfolio = _metric([period[f"top_{k}_return"] for period in result.periods])
        benchmark = expected["benchmark_cumulative_return"]
        expected[f"top_{k}_cumulative_return"] = portfolio
        expected[f"top_{k}_benchmark_relative_return"] = (
            portfolio - benchmark if portfolio is not None and benchmark is not None else None
        )
    expected["benchmark_relative_return"] = expected.get("top_5_benchmark_relative_return")
    for key, value in expected.items():
        actual = result.metrics.get(key)
        if actual is None or value is None:
            if actual is not None or value is not None:
                raise ValueError(
                    f"backtest metric {key} does not match recomputed period values; "
                    "content/hash identity is insufficient"
                )
            continue
        if float(actual) != float(value):
            raise ValueError(
                f"backtest metric {key} does not match recomputed period values; "
                "content/hash identity is insufficient"
            )


def _validate_status_from_periods(result: BacktestResult, top_ks: set[int]) -> None:
    """Bind availability and claim flags to the serialized period rows."""
    metrics = result.metrics
    selected_counts = [
        period[f"top_{k}_selected"]
        for period in result.periods
        for k in top_ks
    ]
    has_eligible_selection = any(count > 0 for count in selected_counts)
    survivor = metrics["evidence_status"] == "descriptive_survivor_selected_evidence"
    if not result.periods or not has_eligible_selection:
        expected_status = "unavailable_insufficient_data"
        expected_unavailable = True
        expected_reason = INSUFFICIENT_DATA_REASON
    else:
        expected_status = "available_descriptive" if survivor else "available"
        expected_unavailable = survivor
        expected_reason = None
    if metrics["availability_status"] != expected_status:
        raise ValueError("backtest availability status does not match serialized periods")
    if metrics["performance_claims_unavailable"] is not expected_unavailable:
        raise ValueError("backtest performance-claims availability does not match serialized periods")
    expected_suppressed = survivor
    for key in (
        "performance_claims_suppressed",
        "benchmark_relative_claims_suppressed",
        "alpha_claim_suppressed",
    ):
        if metrics[key] is not expected_suppressed:
            raise ValueError("backtest suppression flags do not match serialized status")
    if expected_reason is None:
        if "insufficient_data_reason" in metrics:
            raise ValueError("backtest insufficient-data reason is only valid for sufficient data")
    elif metrics.get("insufficient_data_reason") != expected_reason:
        raise ValueError("backtest insufficient-data reason does not match serialized periods")


def _validate_period_turnovers(result: BacktestResult, top_ks: set[int]) -> None:
    """Validate the serialized turnover fields against their set-size basis."""
    previous_counts = {k: 0 for k in top_ks}
    previous_factor_count = 0
    for index, period in enumerate(result.periods):
        for k in top_ks:
            selected = period[f"top_{k}_selected"]
            turnover = float(period[f"top_{k}_turnover"])
            if selected == 0:
                if turnover != 0.0 or period[f"top_{k}_return"] is not None:
                    raise ValueError(f"backtest period {index} has an invalid empty top-{k} selection")
            else:
                numerator = turnover * selected
                if not math.isclose(numerator, round(numerator), rel_tol=0.0, abs_tol=1e-9):
                    raise ValueError(f"backtest period {index} top-{k} turnover is not a set ratio")
                if numerator > selected + previous_counts[k] + 1e-9:
                    raise ValueError(f"backtest period {index} top-{k} turnover exceeds its set basis")
                if index == 0 and not math.isclose(numerator, selected, rel_tol=0.0, abs_tol=1e-9):
                    raise ValueError(f"backtest period {index} top-{k} turnover does not start from empty holdings")
                if period[f"top_{k}_return"] is None:
                    raise ValueError(f"backtest period {index} top-{k} return is missing for a selected portfolio")
            previous_counts[k] = selected
        factor_selected = period["factor_baseline_selected"]
        factor_turnover = float(period["factor_baseline_turnover"])
        if factor_selected == 0:
            if factor_turnover != 0.0 or period["factor_baseline_return"] is not None:
                raise ValueError(f"backtest period {index} has an invalid empty factor selection")
        else:
            numerator = factor_turnover * factor_selected
            if not math.isclose(numerator, round(numerator), rel_tol=0.0, abs_tol=1e-9):
                raise ValueError(f"backtest period {index} factor turnover is not a set ratio")
            if numerator > factor_selected + previous_factor_count + 1e-9:
                raise ValueError(f"backtest period {index} factor turnover exceeds its set basis")
            if index == 0 and not math.isclose(numerator, factor_selected, rel_tol=0.0, abs_tol=1e-9):
                raise ValueError(f"backtest period {index} factor turnover does not start from empty holdings")
            if period["factor_baseline_return"] is None:
                raise ValueError(f"backtest period {index} factor return is missing for a selected portfolio")
        previous_factor_count = factor_selected
    period_dates = [period["decision_date"] for period in result.periods]
    if len(set(period_dates)) != len(period_dates) or period_dates != sorted(period_dates):
        raise ValueError("backtest period dates must be unique and ordered")
    if any(value not in result.validation_dates for value in period_dates):
        raise ValueError("backtest period date is not a requested validation date")


def walk_forward_backtest(
    dataset: Any,
    model: SpecializedRankingModel | None = None,
    *,
    validation_dates: Sequence[date | str] | None = None,
    top_ks: Sequence[int] = (5, 10),
    transaction_cost_bps: object = 10.0,
) -> BacktestResult:
    cost = _validate_transaction_cost_bps(transaction_cost_bps)
    target_horizon = getattr(dataset, "target_horizon", "3m")
    if target_horizon not in {"3m", "6m", "12m"}:
        raise ValueError("dataset target horizon is unsupported")
    if getattr(dataset, "return_basis", "price_return") != "price_return":
        raise ValueError("backtest requires price_return dataset")
    for row in dataset.rows:
        _validate_typed_target_contract(row)
        _validate_monthly_target_contract(row, target_horizon)
        for horizon in ("1m", "3m", "6m", "12m"):
            basis = row.targets.get(f"target_{horizon}_return_basis")
            metadata = getattr(row, "target_metadata", None) or {}
            if basis is None and f"target_{horizon}_return_basis" in metadata:
                basis = metadata[f"target_{horizon}_return_basis"]
            if basis is None and horizon in {"6m", "12m"}:
                continue
            if basis is None:
                raise ValueError("return basis metadata is required")
            if basis != "price_return":
                raise ValueError("mixed return bases are not permitted")
    if not top_ks or any(isinstance(k, bool) or not isinstance(k, int) or k <= 0 for k in top_ks):
        raise ValueError("top_ks must contain positive integers")
    _validate_evaluation_periods(dataset.rows)
    model = model or SpecializedRankingModel()
    bound_artifact = model.artifact
    evaluation_model = SpecializedRankingModel() if bound_artifact is not None else model
    bound_training_window = getattr(bound_artifact, "training_window", None)
    dates = sorted({date.fromisoformat(row.decision_date) for row in dataset.rows})
    validations = [value if isinstance(value, date) else date.fromisoformat(value) for value in (validation_dates or dates[-12:])]
    if validation_dates is not None:
        if not validations:
            raise ValueError("validation dates must not be empty")
    periods: list[dict[str, Any]] = []
    prior = {k: set() for k in top_ks}
    factor_prior: set[str] = set()
    scenario_predictions: list[float] = []
    for training, test, validation in walk_forward_splits(
        dataset.rows, validation_dates=validations, target_horizon=target_horizon,
    ):
        try:
            evaluation_model.fit(
                training,
                training_cutoff=validation,
                dataset_id=dataset.dataset_id,
                history_snapshot_id=dataset.history_snapshot_id,
                benchmark_ticker=dataset.benchmark_ticker,
                target_observable_by=validation,
                target_horizon=target_horizon,
                training_window=bound_training_window,
            )
        except ValueError:
            continue
        eligible = [r for r in test if r.targets.get("target_1m_return") is not None and r.targets.get("target_1m_benchmark_return") is not None and r.targets.get("target_1m_excess_return") is not None]
        ranked = sorted([(evaluation_model.predict(r), r) for r in eligible if evaluation_model.predict(r) is not None], key=lambda x: (-x[0], x[1].ticker))
        scenario_predictions.extend(float(value) for value, _ in ranked)
        factor_ranked = sorted([(factor_score(r), r) for r in eligible if factor_score(r) is not None], key=lambda x: (-x[0], x[1].ticker))
        factor_selected = factor_ranked[:min(DEFAULT_FACTOR_TOP_K, len(factor_ranked))]
        period: dict[str, Any] = {
            "decision_date": validation.isoformat(),
            "benchmark_return": next((r.targets.get("target_1m_benchmark_return") for r in eligible if r.targets.get("target_1m_benchmark_return") is not None), None),
            "factor_baseline_requested": DEFAULT_FACTOR_TOP_K,
            "factor_baseline_selected": len(factor_selected),
            "factor_baseline_tickers": [r.ticker for _, r in factor_selected],
            "turnover_convention": TURNOVER_CONVENTION,
        }
        for k in top_ks:
            selected = ranked[:min(k, len(ranked))]
            holdings = {r.ticker for _, r in selected}
            returns = [r.targets.get("target_1m_return") for _, r in selected]
            finite_returns = [x for x in returns if x is not None and math.isfinite(float(x))]
            raw = sum(finite_returns) / len(finite_returns) if finite_returns else None
            turnover = len(holdings.symmetric_difference(prior[k])) / len(holdings) if holdings else 0.0
            period[f"top_{k}_selected"] = len(holdings)
            period[f"top_{k}_return"] = raw - (cost / 10000) * turnover if raw is not None else None
            period[f"top_{k}_turnover"] = turnover
            prior[k] = holdings
        factor_holdings = {r.ticker for _, r in factor_selected}
        factor_returns = [r.targets.get("target_1m_return") for _, r in factor_selected]
        finite_factor = [x for x in factor_returns if x is not None and math.isfinite(float(x))]
        factor_raw = sum(finite_factor) / len(finite_factor) if finite_factor else None
        factor_turnover = len(factor_holdings.symmetric_difference(factor_prior)) / len(factor_holdings) if factor_holdings else 0.0
        period["factor_baseline_return"] = factor_raw - (cost / 10000) * factor_turnover if factor_raw is not None else None
        period["factor_baseline_turnover"] = factor_turnover
        factor_prior = factor_holdings
        periods.append(period)
    benchmark_cumulative = _metric([p.get("benchmark_return") for p in periods])
    metrics = {f"top_{k}_cumulative_return": _metric([p.get(f"top_{k}_return") for p in periods]) for k in top_ks}
    metrics.update({
        "benchmark_cumulative_return": benchmark_cumulative,
        "transaction_cost_bps": cost,
        "turnover_convention": TURNOVER_CONVENTION,
        "portfolio_return_basis": PORTFOLIO_RETURN_BASIS,
        "benchmark_return_basis": PORTFOLIO_RETURN_BASIS,
        "portfolio_interval_convention": PORTFOLIO_INTERVAL_CONVENTION,
    })
    for k in top_ks:
        portfolio_cumulative = metrics[f"top_{k}_cumulative_return"]
        metrics[f"top_{k}_benchmark_relative_return"] = portfolio_cumulative - benchmark_cumulative if portfolio_cumulative is not None and benchmark_cumulative is not None else None
    metrics["benchmark_relative_return"] = metrics.get("top_5_benchmark_relative_return")
    survivor = getattr(dataset, "membership_evidence_status", "") == "descriptive_survivor_selected_evidence" or any(bool(getattr(row, "target_metadata", None) and row.target_metadata.get("current_snapshot_only")) for row in dataset.rows)
    metrics["evidence_status"] = "descriptive_survivor_selected_evidence" if survivor else "point_in_time_membership_evidence"
    metrics["performance_claims_suppressed"] = survivor
    metrics["performance_claims_unavailable"] = survivor
    metrics["benchmark_relative_claims_suppressed"] = survivor
    metrics["alpha_claim_suppressed"] = survivor
    metrics["availability_status"] = "available_descriptive" if survivor else "available"
    if survivor:
        for key in list(metrics):
            normalized = key.casefold()
            if normalized in {"alpha_claim_suppressed", "benchmark_relative_claims_suppressed"}:
                continue
            if "alpha" in normalized or "benchmark_relative" in normalized or normalized.endswith(("cumulative_return", "annualized_return", "max_drawdown", "volatility", "hit_rate", "rank_ic")):
                metrics[key] = None
    visible_periods = [] if survivor else periods
    if not visible_periods or not any(
        period.get(f"top_{top_ks[0]}_selected", 0) for period in visible_periods
    ):
        metrics["performance_claims_unavailable"] = True
        metrics["availability_status"] = "unavailable_insufficient_data"
        metrics["insufficient_data_reason"] = INSUFFICIENT_DATA_REASON
    artifact = bound_artifact or getattr(evaluation_model, "artifact", None)
    scenarios = build_scenarios(sum(scenario_predictions) / len(scenario_predictions) if scenario_predictions else None, getattr(artifact, "residual_std", None), evidence_quality="insufficient" if not visible_periods else "medium")
    result = BacktestResult(
        visible_periods, metrics, "price_return", dataset.dataset_id, dataset.history_snapshot_id,
        dataset.benchmark_ticker, tuple(dataset.feature_schema), MODEL_VERSION, target_horizon,
        getattr(artifact, "training_cutoff", validations[-1].isoformat() if validations else None),
        bound_training_window,
        tuple(value.isoformat() if isinstance(value, date) else value for value in validations), cost,
        getattr(artifact, "model_id", None), scenarios, BACKTEST_SCHEMA_VERSION, "",
        getattr(artifact, "missing_feature_policy", None),
    )
    return _with_backtest_identity(result)


def save_backtest(result: BacktestResult, path) -> None:
    _validate_backtest(result)
    expected = _with_backtest_identity(BacktestResult(**{**asdict(result), "backtest_id": ""}))
    if result.backtest_id != expected.backtest_id:
        raise ValueError("backtest content/hash does not match backtest identity")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(result), sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_backtest(path) -> BacktestResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != set(BacktestResult.__dataclass_fields__):
        raise ValueError("backtest has an invalid field set")
    result = BacktestResult(
        payload["periods"], payload["metrics"], payload["return_basis"], payload["dataset_id"],
        payload["history_snapshot_id"], payload["benchmark_ticker"], tuple(payload["feature_schema"]),
        payload["model_version"], payload["target_horizon"], payload["training_cutoff"],
        payload["training_window"], tuple(payload["validation_dates"]), payload["transaction_cost_bps"],
        payload["model_artifact_id"], payload["scenarios"], payload["schema_version"], payload["backtest_id"],
        payload["missing_feature_policy"],
    )
    _validate_backtest(result)
    expected = _with_backtest_identity(BacktestResult(**{**asdict(result), "backtest_id": ""}))
    if result.backtest_id != expected.backtest_id:
        raise ValueError("backtest content/hash does not match backtest identity")
    return result
