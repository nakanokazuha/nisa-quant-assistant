"""Safe deterministic Phase 3 Markdown/JSON report rendering."""
from __future__ import annotations
import hashlib
import json, math
import re
from dataclasses import asdict, is_dataclass
from datetime import date
from typing import Any, Mapping
from .report_rendering import validate_report_safety
from .ranking_model import current_prediction_freshness


PORTFOLIO_INTERVAL_CONVENTION = (
    "non-overlapping [start,end) monthly interval ending at the next monthly decision date"
)

def _plain(value, *, nested: bool = False):
    if is_dataclass(value):
        return _plain(asdict(value), nested=nested)
    if isinstance(value, dict):
        return {key: _plain(item, nested=True) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item, nested=True) for item in value]
    if nested and hasattr(value, "__dict__"):
        return {key: _plain(item, nested=True) for key, item in vars(value).items()}
    return value

def _finite(value):
    if isinstance(value,float) and not math.isfinite(value): raise ValueError("report contains a non-finite value")
    if isinstance(value,dict):
        for x in value.values(): _finite(x)
    elif isinstance(value,(list,tuple)):
        for x in value: _finite(x)


def _required_string(value: dict[str,Any], key: str) -> str:
    result=value.get(key)
    if not isinstance(result,str) or not result:
        raise ValueError(f"Phase 3 artifact field {key} is required")
    return result


def _required_schema(value: dict[str, Any], key: str) -> tuple[str, ...]:
    raw = value.get(key)
    if not isinstance(raw, (list, tuple)) or not raw or any(not isinstance(item, str) or not item for item in raw):
        raise ValueError(f"Phase 3 artifact field {key} is required")
    return tuple(raw)


def _validated_dataset(dataset: dict[str, Any]) -> Any | None:
    """Validate a serialized full dataset and return its typed artifact."""
    from .training_dataset import PanelRow, TrainingDataset, _identity, _validate_dataset

    if set(dataset) != set(TrainingDataset.__dataclass_fields__):
        return None
    raw_rows = dataset.get("rows")
    if not isinstance(raw_rows, list) or any(
        not isinstance(row, dict) or set(row) != set(PanelRow.__dataclass_fields__)
        for row in raw_rows
    ):
        raise ValueError("Phase 3 dataset rows are invalid")
    try:
        rows = [PanelRow(**row) for row in raw_rows]
        typed = TrainingDataset(
            rows=rows,
            dataset_id=dataset["dataset_id"],
            history_snapshot_id=dataset["history_snapshot_id"],
            benchmark_ticker=dataset["benchmark_ticker"],
            feature_schema=tuple(dataset["feature_schema"]),
            return_basis=dataset["return_basis"],
            sec_source_ids=tuple(dataset["sec_source_ids"]),
            sec_context_ids=tuple(dataset["sec_context_ids"]),
            membership_evidence_status=dataset["membership_evidence_status"],
            schema_version=dataset["schema_version"],
            target_horizon=dataset["target_horizon"],
        )
        _validate_dataset(typed)
        expected = _identity(
            rows, typed.history_snapshot_id, typed.benchmark_ticker,
            typed.feature_schema, typed.return_basis, typed.sec_source_ids,
            typed.sec_context_ids, typed.membership_evidence_status, typed.target_horizon,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Phase 3 dataset values are invalid") from exc
    if typed.dataset_id != expected:
        raise ValueError("Phase 3 dataset content/hash does not match dataset identity")
    return typed


def _validated_dataset_rows(dataset: dict[str, Any]) -> list[Any] | None:
    """Validate a serialized full dataset and return typed rows when present."""
    typed = _validated_dataset(dataset)
    return typed.rows if typed is not None else None


def _provenance_dataset_rows(dataset: dict[str, Any]) -> list[Any] | None:
    """Coerce row-shaped in-memory datasets for training provenance checks."""
    from .training_dataset import PanelRow

    raw_rows = dataset.get("rows")
    if not isinstance(raw_rows, list) or any(
        not isinstance(row, dict) or set(row) != set(PanelRow.__dataclass_fields__)
        for row in raw_rows
    ):
        return None
    try:
        return [PanelRow(**row) for row in raw_rows]
    except (TypeError, ValueError):
        return None


def _supports_dataset_content_validation(value: Any) -> bool:
    """Return whether the input carries the canonical serialized row shape."""
    from .training_dataset import PanelRow, TrainingDataset

    if isinstance(value, dict):
        return set(value) == set(TrainingDataset.__dataclass_fields__)
    return isinstance(value, TrainingDataset) and all(
        isinstance(row, PanelRow) for row in value.rows
    )


def _validate_serialized_backtest(backtest: dict[str, Any]) -> None:
    """Validate a complete BacktestResult before any report projection."""
    from .walk_forward_evaluation import BacktestResult, _validate_backtest

    if set(backtest) != set(BacktestResult.__dataclass_fields__):
        metrics = backtest.get("metrics")
        periods = backtest.get("periods")
        if not isinstance(metrics, dict) or not isinstance(periods, list):
            raise ValueError("Phase 3 backtest status is not validated")
        status = metrics.get("availability_status")
        if status not in {"available", "available_descriptive", "unavailable_insufficient_data"}:
            raise ValueError("Phase 3 backtest status is not validated")
        selected = any(
            isinstance(period, dict)
            and any(
                isinstance(key, str) and key.startswith("top_") and key.endswith("_return")
                and period.get(key) is not None
                for key in period
            )
            for period in periods
        )
        expected = "available_descriptive" if (
            metrics.get("evidence_status") == "descriptive_survivor_selected_evidence" and selected
        ) else "available" if selected else "unavailable_insufficient_data"
        if status != expected:
            raise ValueError("Phase 3 backtest status is not validated")
        return
    try:
        typed = BacktestResult(
            periods=backtest["periods"], metrics=backtest["metrics"],
            return_basis=backtest["return_basis"], dataset_id=backtest["dataset_id"],
            history_snapshot_id=backtest["history_snapshot_id"],
            benchmark_ticker=backtest["benchmark_ticker"],
            feature_schema=tuple(backtest["feature_schema"]),
            model_version=backtest["model_version"], target_horizon=backtest["target_horizon"],
            training_cutoff=backtest["training_cutoff"], training_window=backtest["training_window"],
            validation_dates=tuple(backtest["validation_dates"]),
            transaction_cost_bps=backtest["transaction_cost_bps"],
            model_artifact_id=backtest["model_artifact_id"], scenarios=backtest["scenarios"],
            schema_version=backtest["schema_version"], backtest_id=backtest["backtest_id"],
            missing_feature_policy=backtest["missing_feature_policy"],
        )
        _validate_backtest(typed)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Phase 3 backtest values are invalid") from exc


def _bind_artifacts(
    dataset: dict[str, Any], model: dict[str, Any], backtest: dict[str, Any],
    *, validate_dataset_content: bool = True,
) -> dict[str, Any]:
    dataset_binding = {
        "dataset_id": _required_string(dataset, "dataset_id"),
        "history_snapshot_id": _required_string(dataset, "history_snapshot_id"),
        "benchmark_ticker": _required_string(dataset, "benchmark_ticker"),
        "feature_schema": _required_schema(dataset, "feature_schema"),
        "return_basis": _required_string(dataset, "return_basis"),
        "target_horizon": dataset.get("target_horizon", "3m"),
    }
    model_binding = {
        "model_version": _required_string(model, "model_version"),
        "dataset_id": _required_string(model, "dataset_id"),
        "history_snapshot_id": _required_string(model, "history_snapshot_id"),
        "feature_schema": _required_schema(model, "feature_schema"),
        "return_basis": _required_string(model, "return_basis"),
        "missing_feature_policy": model.get("missing_feature_policy"),
    }
    backtest_binding = {
        "model_version": _required_string(backtest, "model_version"),
        "dataset_id": _required_string(backtest, "dataset_id"),
        "history_snapshot_id": _required_string(backtest, "history_snapshot_id"),
        "benchmark_ticker": _required_string(backtest, "benchmark_ticker"),
        "feature_schema": _required_schema(backtest, "feature_schema"),
        "return_basis": _required_string(backtest, "return_basis"),
        "missing_feature_policy": backtest.get("missing_feature_policy"),
    }
    if dataset_binding["return_basis"] != "price_return":
        raise ValueError("Phase 3 report requires price_return artifacts")
    typed_dataset_rows = (
        _validated_dataset_rows(dataset)
        if validate_dataset_content else _provenance_dataset_rows(dataset)
    )
    for field in ("dataset_id", "history_snapshot_id", "feature_schema", "return_basis"):
        if model_binding[field] != dataset_binding[field]:
            raise ValueError(f"model {field.replace('_', ' ')} does not match dataset")
        if backtest_binding[field] != dataset_binding[field]:
            raise ValueError(f"backtest {field.replace('_', ' ')} does not match dataset")
    if backtest_binding["benchmark_ticker"] != dataset_binding["benchmark_ticker"]:
        raise ValueError("benchmark identity does not match dataset")
    if backtest_binding["model_version"] != model_binding["model_version"]:
        raise ValueError("backtest model version does not match model")
    optional_model_benchmark = model.get("benchmark_ticker")
    if optional_model_benchmark is not None and optional_model_benchmark != dataset_binding["benchmark_ticker"]:
        raise ValueError("model benchmark identity does not match dataset")
    # Dataclass backtest objects are also accepted by the historical
    # descriptive-only renderer path. Strict Phase 3 binding starts when a
    # real model artifact exposes its content-derived identity.
    strict_artifacts = (
        any(key in model for key in ("model_id", "content_hash", "coefficients"))
    )
    if strict_artifacts:
        from .ranking_model import RankingModelArtifact, SpecializedRankingModel
        from .ranking_model import _count_usable_training_rows, _validate_artifact
        from .walk_forward_evaluation import BacktestResult, _validate_backtest

        if set(model) != set(RankingModelArtifact.__dataclass_fields__):
            raise ValueError("model artifact field set is invalid")
        if not typed_dataset_rows:
            raise ValueError("Phase 3 model provenance requires typed dataset rows")
        if set(backtest) != set(BacktestResult.__dataclass_fields__):
            raise ValueError("backtest artifact field set is invalid")
        try:
            model_artifact = RankingModelArtifact(
                model_version=model["model_version"], feature_schema=tuple(model["feature_schema"]),
                training_cutoff=model["training_cutoff"], dataset_id=model["dataset_id"],
                history_snapshot_id=model["history_snapshot_id"], coefficients=tuple(model["coefficients"]),
                intercept=model["intercept"], means=tuple(model["means"]), scales=tuple(model["scales"]),
                residual_std=model["residual_std"], training_rows=model["training_rows"],
                target_horizon=model["target_horizon"], return_basis=model["return_basis"],
                benchmark_ticker=model["benchmark_ticker"], training_window=model["training_window"],
                schema_version=model["schema_version"], model_id=model["model_id"],
                content_hash=model["content_hash"], imputation_values=tuple(model["imputation_values"]),
                missing_feature_policy=model["missing_feature_policy"],
            )
            _validate_artifact(model_artifact)
            canonical_model = SpecializedRankingModel().fit(
                typed_dataset_rows,
                training_cutoff=model_artifact.training_cutoff,
                target_horizon=model_artifact.target_horizon,
                training_window=model_artifact.training_window,
                dataset_id=dataset_binding["dataset_id"],
                history_snapshot_id=dataset_binding["history_snapshot_id"],
                benchmark_ticker=dataset_binding["benchmark_ticker"],
                target_observable_by=model_artifact.training_cutoff,
            )
            for field in RankingModelArtifact.__dataclass_fields__:
                if getattr(model_artifact, field) != getattr(canonical_model, field):
                    raise ValueError(
                        f"model artifact field {field} does not match canonical refit"
                    )
            if typed_dataset_rows is not None:
                expected_training_rows = _count_usable_training_rows(
                    typed_dataset_rows,
                    training_cutoff=model_artifact.training_cutoff,
                    target_horizon=model_artifact.target_horizon,
                    training_window=model_artifact.training_window,
                )
                if model_artifact.training_rows != expected_training_rows:
                    raise ValueError("model artifact training-row provenance is invalid")
            backtest_artifact = BacktestResult(
                periods=backtest["periods"], metrics=backtest["metrics"],
                return_basis=backtest["return_basis"], dataset_id=backtest["dataset_id"],
                history_snapshot_id=backtest["history_snapshot_id"], benchmark_ticker=backtest["benchmark_ticker"],
                feature_schema=tuple(backtest["feature_schema"]), model_version=backtest["model_version"],
                target_horizon=backtest["target_horizon"], training_cutoff=backtest["training_cutoff"],
                training_window=backtest["training_window"], validation_dates=tuple(backtest["validation_dates"]),
                transaction_cost_bps=backtest["transaction_cost_bps"],
                model_artifact_id=backtest["model_artifact_id"], scenarios=backtest["scenarios"],
                schema_version=backtest["schema_version"], backtest_id=backtest["backtest_id"],
                missing_feature_policy=backtest["missing_feature_policy"],
            )
            _validate_backtest(backtest_artifact)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Phase 3 artifact values are invalid") from exc
        required_model = ("model_id", "content_hash", "target_horizon", "training_cutoff", "training_window", "benchmark_ticker", "return_basis", "schema_version", "missing_feature_policy")
        required_backtest = ("backtest_id", "model_artifact_id", "target_horizon", "training_cutoff", "training_window", "validation_dates", "transaction_cost_bps", "schema_version", "scenarios", "missing_feature_policy")
        for key in required_model:
            if key not in model:
                raise ValueError(f"model artifact field {key} is required")
        for key in required_backtest:
            if key not in backtest:
                raise ValueError(f"backtest artifact field {key} is required")
        if model["schema_version"] != 1 or backtest["schema_version"] != 1:
            raise ValueError("Phase 3 artifact schema version is unsupported")
        if model["model_id"] != backtest["model_artifact_id"]:
            raise ValueError("backtest model artifact does not match model")
        model_payload = dict(model)
        model_payload.pop("model_id", None)
        model_payload.pop("content_hash", None)
        model_hash = hashlib.sha256(
            json.dumps(model_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        if model["content_hash"] != model_hash or model["model_id"] != f"model-{model_hash[:20]}":
            raise ValueError("model artifact content/hash does not match model identity")
        backtest_payload = dict(backtest)
        backtest_payload.pop("backtest_id", None)
        backtest_hash = hashlib.sha256(
            json.dumps(backtest_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        if backtest["backtest_id"] != f"backtest-{backtest_hash[:20]}":
            raise ValueError("backtest content/hash does not match backtest identity")
        if model["missing_feature_policy"] != backtest["missing_feature_policy"]:
            raise ValueError("missing-feature policy does not match between model and backtest")
        for key in ("target_horizon", "training_cutoff", "training_window"):
            if model[key] != backtest[key]:
                raise ValueError(f"{key.replace('_', ' ')} does not match between model and backtest")
        if model["target_horizon"] != dataset_binding["target_horizon"]:
            raise ValueError("target horizon does not match dataset")
        if model["benchmark_ticker"] != dataset_binding["benchmark_ticker"]:
            raise ValueError("model benchmark identity does not match dataset")
        dates = backtest["validation_dates"]
        if not isinstance(dates, (list, tuple)) or len(set(dates)) != len(dates):
            raise ValueError("backtest validation dates are invalid or duplicated")
        cost = backtest["transaction_cost_bps"]
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(float(cost)) or float(cost) < 0:
            raise ValueError("backtest transaction cost is invalid")
    return {
        "dataset_id": dataset_binding["dataset_id"],
        "history_snapshot_id": dataset_binding["history_snapshot_id"],
        "model_version": model_binding["model_version"],
        "benchmark_ticker": dataset_binding["benchmark_ticker"],
        "feature_schema": list(dataset_binding["feature_schema"]),
        "return_basis": dataset_binding["return_basis"],
        "target_horizon": dataset_binding["target_horizon"],
        **({
            "model_id": model.get("model_id"),
            "model_content_hash": model.get("content_hash"),
            "backtest_id": backtest.get("backtest_id"),
            "target_horizon": model.get("target_horizon", dataset.get("target_horizon", "3m")),
            "training_cutoff": model.get("training_cutoff"),
            "training_window": model.get("training_window"),
            "validation_dates": list(backtest.get("validation_dates", ())),
            "transaction_cost_bps": backtest.get("transaction_cost_bps", backtest.get("metrics", {}).get("transaction_cost_bps")),
            "missing_feature_policy": model.get("missing_feature_policy"),
        } if strict_artifacts else {}),
    }


def _dataset_has_current_only_membership(dataset: dict[str,Any]) -> bool:
    rows=dataset.get("rows",[])
    if not isinstance(rows,list):
        raise ValueError("Phase 3 dataset rows must be a list")
    return any(
        isinstance(row,dict)
        and isinstance(row.get("target_metadata"),dict)
        and row["target_metadata"].get("current_snapshot_only") is True
        for row in rows
    )


def _validate_dataset_bound_backtest_status(
    dataset: dict[str, Any], backtest: dict[str, Any],
) -> None:
    """Prevent a serialized report from reclassifying membership evidence."""
    metrics = backtest.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("Phase 3 backtest metrics must be an object")
    survivor = (
        _dataset_has_current_only_membership(dataset)
        or dataset.get("membership_evidence_status") == "descriptive_survivor_selected_evidence"
    )
    expected_evidence = (
        "descriptive_survivor_selected_evidence" if survivor
        else "point_in_time_membership_evidence"
    )
    if metrics.get("evidence_status") != expected_evidence:
        raise ValueError("backtest evidence status does not match dataset membership")
    periods = backtest.get("periods")
    has_selection = isinstance(periods, list) and any(
        isinstance(period, dict)
        and any(
            isinstance(key, str) and key.startswith("top_") and key.endswith("_return")
            and period.get(key) is not None
            for key in period
        )
        for period in periods
    )
    expected_status = (
        "available_descriptive" if survivor and has_selection
        else "unavailable_insufficient_data" if not has_selection
        else metrics.get("availability_status")
    )
    if metrics.get("availability_status") != expected_status:
        raise ValueError("backtest availability status does not match dataset membership")


def _validate_bound_backtest_periods(
    dataset: dict[str, Any], model: dict[str, Any], backtest: dict[str, Any],
) -> None:
    """Recompute every strict backtest field from its bound typed inputs."""
    from .ranking_model import RankingModelArtifact, SpecializedRankingModel, _validate_artifact
    from .walk_forward_evaluation import _TOP_K_METRIC_PATTERN, walk_forward_backtest

    typed_dataset = _validated_dataset(dataset)
    if typed_dataset is None:
        raise ValueError("Phase 3 backtest canonical validation requires a serialized dataset")
    try:
        model_artifact = RankingModelArtifact(
            model_version=model["model_version"], feature_schema=tuple(model["feature_schema"]),
            training_cutoff=model["training_cutoff"], dataset_id=model["dataset_id"],
            history_snapshot_id=model["history_snapshot_id"], coefficients=tuple(model["coefficients"]),
            intercept=model["intercept"], means=tuple(model["means"]), scales=tuple(model["scales"]),
            residual_std=model["residual_std"], training_rows=model["training_rows"],
            target_horizon=model["target_horizon"], return_basis=model["return_basis"],
            benchmark_ticker=model["benchmark_ticker"], training_window=model["training_window"],
            schema_version=model["schema_version"], model_id=model["model_id"],
            content_hash=model["content_hash"], imputation_values=tuple(model["imputation_values"]),
            missing_feature_policy=model["missing_feature_policy"],
        )
        _validate_artifact(model_artifact)
        top_ks = sorted({
            int(match.group(1))
            for key in backtest["metrics"]
            if isinstance(key, str)
            for match in [_TOP_K_METRIC_PATTERN.fullmatch(key)]
            if match
        })
        canonical = walk_forward_backtest(
            typed_dataset,
            model=SpecializedRankingModel(model_artifact),
            validation_dates=tuple(backtest["validation_dates"]),
            top_ks=tuple(top_ks),
            transaction_cost_bps=backtest["transaction_cost_bps"],
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Phase 3 backtest canonical validation inputs are invalid") from exc
    if _plain(canonical) != backtest:
        raise ValueError("Phase 3 serialized backtest does not match canonical recomputation")


def _is_performance_claim_key(key: str) -> bool:
    normalized_key = key.casefold()
    if normalized_key in {"alpha", "return", "benchmark_return"}:
        return True
    if any(token in normalized_key for token in ("excess", "benchmark_relative")):
        return True
    return normalized_key.endswith("_return") or normalized_key.endswith(
        ("cumulative_return", "annualized_return", "max_drawdown", "volatility", "hit_rate", "rank_ic")
    )


def _validate_current_predictions(
    predictions: Any, dataset: dict[str, Any], model: dict[str, Any], survivor: bool,
    as_of: date | str | None, *, validate_dataset_content: bool = True,
) -> list[dict[str, Any]]:
    if not isinstance(predictions, list):
        raise ValueError("Phase 3 current predictions must be a list")
    expected_fields = {
        "ticker", "rank", "model_score", "predicted_3m_excess_return", "target_horizon",
        "model_id", "dataset_id", "history_snapshot_id", "benchmark", "return_basis",
        "decision_date", "evidence_status", "freshness_evidence",
    }
    dataset_rows = dataset.get("rows", [])
    if not isinstance(dataset_rows, list):
        raise ValueError("Phase 3 dataset rows must be a list")
    row_keys = {
        (row.get("ticker"), row.get("decision_date"))
        for row in dataset_rows if isinstance(row, dict)
    }
    if as_of is None:
        eligible_dates = [
            date.fromisoformat(row["decision_date"])
            for row in dataset_rows
            if isinstance(row, dict) and isinstance(row.get("decision_date"), str)
        ]
    else:
        cutoff = as_of if isinstance(as_of, date) else date.fromisoformat(as_of)
        eligible_dates = [
            date.fromisoformat(row["decision_date"])
            for row in dataset_rows
            if isinstance(row, dict)
            and isinstance(row.get("decision_date"), str)
            and date.fromisoformat(row["decision_date"]) <= cutoff
        ]
    latest_date = max(eligible_dates) if eligible_dates else None
    evidence_status = "descriptive_survivor_selected_evidence" if survivor else "point_in_time_membership_evidence"
    normalized: list[dict[str, Any]] = []
    for prediction in predictions:
        if not isinstance(prediction, dict) or set(prediction) != expected_fields:
            raise ValueError("Phase 3 current prediction fields are invalid")
        if not isinstance(prediction["ticker"], str) or not prediction["ticker"]:
            raise ValueError("Phase 3 current prediction ticker is invalid")
        rank = prediction["rank"]
        if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
            raise ValueError("Phase 3 current prediction rank is invalid")
        for key in ("model_score", "predicted_3m_excess_return"):
            value = prediction[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError("Phase 3 current prediction score is invalid")
        for key in ("target_horizon", "model_id", "dataset_id", "history_snapshot_id", "benchmark", "return_basis", "decision_date", "evidence_status"):
            if not isinstance(prediction[key], str) or not prediction[key]:
                raise ValueError(f"Phase 3 current prediction field {key} is invalid")
        try:
            date.fromisoformat(prediction["decision_date"])
        except ValueError as exc:
            raise ValueError("Phase 3 current prediction decision date is invalid") from exc
        if (prediction["ticker"], prediction["decision_date"]) not in row_keys:
            raise ValueError("Phase 3 current prediction is not bound to dataset rows")
        if prediction["model_id"] != model.get("model_id"):
            raise ValueError("Phase 3 current prediction model does not match model artifact")
        if prediction["dataset_id"] != dataset.get("dataset_id") or prediction["history_snapshot_id"] != dataset.get("history_snapshot_id"):
            raise ValueError("Phase 3 current prediction dataset binding does not match")
        if prediction["benchmark"] != dataset.get("benchmark_ticker") or prediction["return_basis"] != dataset.get("return_basis"):
            raise ValueError("Phase 3 current prediction market binding does not match")
        if prediction["target_horizon"] != model.get("target_horizon", dataset.get("target_horizon", "3m")):
            raise ValueError("Phase 3 current prediction target horizon does not match")
        if prediction["model_score"] != prediction["predicted_3m_excess_return"]:
            raise ValueError("Phase 3 current prediction score fields do not match")
        if prediction["evidence_status"] != evidence_status:
            raise ValueError("Phase 3 current prediction evidence status does not match")
        if latest_date is None or prediction["decision_date"] != latest_date.isoformat():
            raise ValueError("Phase 3 current prediction must use the latest eligible decision date")
        matching_row = next(
            (
                row for row in dataset_rows
                if isinstance(row, dict)
                and row.get("ticker") == prediction["ticker"]
                and row.get("decision_date") == prediction["decision_date"]
            ),
            None,
        )
        expected_freshness = current_prediction_freshness(
            matching_row, as_of or latest_date
        ) if matching_row is not None else None
        if prediction["freshness_evidence"] != expected_freshness:
            raise ValueError("Phase 3 current prediction freshness evidence does not match")
        normalized.append(prediction)
    if len({(item["ticker"], item["decision_date"]) for item in normalized}) != len(normalized):
        raise ValueError("Phase 3 current predictions contain duplicate rows")
    expected_ranks = list(range(1, len(normalized) + 1))
    if [item["rank"] for item in normalized] != expected_ranks:
        raise ValueError("Phase 3 current prediction ranks are invalid")
    if normalized != sorted(normalized, key=lambda item: (-float(item["model_score"]), item["ticker"])):
        raise ValueError("Phase 3 current predictions are not deterministically sorted")
    if "coefficients" in model and validate_dataset_content:
        from .ranking_model import RankingModelArtifact, _validate_artifact
        try:
            artifact = RankingModelArtifact(
                model_version=model["model_version"], feature_schema=tuple(model["feature_schema"]),
                training_cutoff=model["training_cutoff"], dataset_id=model["dataset_id"],
                history_snapshot_id=model["history_snapshot_id"], coefficients=tuple(model["coefficients"]),
                intercept=model["intercept"], means=tuple(model["means"]), scales=tuple(model["scales"]),
                residual_std=model["residual_std"], training_rows=model["training_rows"],
                target_horizon=model["target_horizon"], return_basis=model["return_basis"],
                benchmark_ticker=model["benchmark_ticker"], training_window=model["training_window"],
                schema_version=model["schema_version"], model_id=model["model_id"],
                content_hash=model["content_hash"], imputation_values=tuple(model["imputation_values"]),
                missing_feature_policy=model["missing_feature_policy"],
            )
            _validate_artifact(artifact)
            typed_rows = _validated_dataset_rows(dataset)
            if typed_rows is None:
                raise ValueError("Phase 3 current predictions require a serialized dataset")
            latest_rows = [
                row for row in typed_rows
                if row.decision_date == latest_date.isoformat()
            ]
            expected: list[dict[str, Any]] = []
            scored: list[tuple[float, Any]] = []
            for row in latest_rows:
                if current_prediction_freshness(row, as_of or latest_date).get("status") != "fresh":
                    continue
                score = artifact.predict_row(row)
                if score is not None and math.isfinite(float(score)):
                    scored.append((float(score), row))
            scored.sort(key=lambda item: (-item[0], item[1].ticker))
            expected = [
                {
                    "ticker": row.ticker, "rank": rank, "model_score": score,
                    "predicted_3m_excess_return": score, "target_horizon": artifact.target_horizon,
                    "model_id": artifact.model_id, "dataset_id": artifact.dataset_id,
                    "history_snapshot_id": artifact.history_snapshot_id,
                    "benchmark": artifact.benchmark_ticker, "return_basis": artifact.return_basis,
                    "decision_date": latest_date.isoformat(), "evidence_status": evidence_status,
                    "freshness_evidence": current_prediction_freshness(row, as_of or latest_date),
                }
                for rank, (score, row) in enumerate(scored, start=1)
            ]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Phase 3 current prediction model binding is invalid") from exc
        if normalized != expected:
            raise ValueError("Phase 3 current predictions do not match recomputed model output")
    return normalized


def render_phase3_report(
    *, dataset: Any, model: Any, backtest: Any, output_format: str = "markdown",
    current_predictions: Any | None = None, current_predictions_reason: str | None = None,
    current_predictions_as_of: date | str | None = None,
):
    if output_format not in {"json", "markdown"}:
        raise ValueError("Phase 3 report format must be json or markdown")
    validate_dataset_content = _supports_dataset_content_validation(dataset)
    dataset = _plain(dataset); model = _plain(model); backtest = _plain(backtest)
    if not all(isinstance(value,dict) for value in (dataset, model, backtest)):
        raise ValueError("Phase 3 report artifacts must be objects")
    _validate_serialized_backtest(backtest)
    binding = _bind_artifacts(
        dataset, model, backtest, validate_dataset_content=validate_dataset_content,
    )
    if validate_dataset_content and any(
        key in model for key in ("model_id", "content_hash", "coefficients")
    ):
        _validate_dataset_bound_backtest_status(dataset, backtest)
        _validate_bound_backtest_periods(dataset, model, backtest)
    metrics=backtest.get("metrics",{})
    if not isinstance(metrics,dict): raise ValueError("Phase 3 backtest metrics must be an object")
    survivor=(
        _dataset_has_current_only_membership(dataset)
        or dataset.get("membership_evidence_status") == "descriptive_survivor_selected_evidence"
        or metrics.get("evidence_status") == "descriptive_survivor_selected_evidence"
        or metrics.get("performance_claims_suppressed") is True
    )
    normalized_current_predictions = (
        _validate_current_predictions(
            _plain(current_predictions), dataset, model, survivor, current_predictions_as_of,
            validate_dataset_content=validate_dataset_content,
        )
        if current_predictions is not None else []
    )
    if current_predictions is not None and not normalized_current_predictions and not current_predictions_reason:
        raise ValueError("current predictions reason is required when no candidates are available")
    if current_predictions_reason is not None and (not isinstance(current_predictions_reason, str) or not current_predictions_reason):
        raise ValueError("current predictions reason must be a non-empty string")
    report_backtest=dict(backtest)
    if survivor:
        report_backtest["periods"]=[]
        safe_metrics=dict(report_backtest.get("metrics",{}))
        for key in list(safe_metrics):
            normalized_key=key.casefold()
            if _is_performance_claim_key(key):
                del safe_metrics[key]
        report_backtest["metrics"]=safe_metrics
    interval_convention = metrics.get("portfolio_interval_convention", PORTFOLIO_INTERVAL_CONVENTION)
    if not isinstance(interval_convention, str) or "[start,end)" not in interval_convention or "next monthly decision date" not in interval_convention:
        raise ValueError("Phase 3 report interval convention is missing or invalid")
    backtest_status = metrics.get("availability_status")
    if backtest_status not in {"available", "available_descriptive", "unavailable_insufficient_data"}:
        raise ValueError("Phase 3 backtest status is not validated")
    report_status = backtest_status
    payload={
        "status": report_status,
        "dataset": dataset, "model": model, "backtest": report_backtest,
        "current_predictions": normalized_current_predictions,
        "current_predictions_reason": current_predictions_reason,
        "descriptive_current_survivor_ranking": survivor,
        "performance_claims_suppressed": survivor,
        "counter_evidence": "not_calculated",
        "invalidation": "not_calculated",
        "artifact_binding": binding,
        "evidence_status": "descriptive_survivor_selected_evidence" if survivor else "point_in_time_membership_evidence",
        "manual_only": True,
        "scenario_interpretation": "bear/base/bull ranges are uncertainty heuristics, not calibrated probabilities",
        "sec_interpretation": "SEC Company Facts are optional and may be partial; missing facts remain explicit and are not issuer-value substitutes",
    }
    _finite(payload)
    if output_format=="json":
        validate_report_safety(json.dumps(payload,ensure_ascii=False,sort_keys=True,allow_nan=False))
        return payload
    text="\n".join([
        "# NISA Quant Assistant Phase 3 Report", "", "## Artifact binding", "",
        f"- Dataset: `{dataset.get('dataset_id')}`", f"- History: `{dataset.get('history_snapshot_id')}`", f"- Model: `{model.get('model_version')}`", f"- Benchmark: `{dataset.get('benchmark_ticker')}`", f"- Feature schema: `{', '.join(dataset.get('feature_schema',()))}`", "",
        "## Status", "", f"- Status: `{payload['status']}`", "",
        "## Evaluation", "", f"- Return basis: `{binding['return_basis']}`", f"- Portfolio interval convention: `{interval_convention}`", f"- Metrics: `{json.dumps(report_backtest.get('metrics',{}), sort_keys=True, allow_nan=False)}`", "",
        "## Current prediction output", "", f"- Descriptive current survivor ranking: `{str(survivor).lower()}`", f"- Performance claims suppressed: `{str(survivor).lower()}`", f"- Predictions: `{json.dumps(normalized_current_predictions, sort_keys=True, allow_nan=False)}`", *( [f"- Empty-list reason: `{current_predictions_reason}`"] if current_predictions_reason else [] ), "",
        "Counter-evidence: `not_calculated`; invalidation: `not_calculated`.", "",
        "## Scenario interpretation", "", "Bear/base/bull ranges are uncertainty heuristics, not calibrated probabilities.", "",
        "## SEC coverage", "", "SEC Company Facts are optional and may be partial; missing facts remain explicit and are not issuer-value substitutes.", "",
        "## Evidence limitation", "", "Descriptive survivor-selected evidence only; benchmark-relative and performance claims are suppressed." if survivor else "Point-in-time membership evidence is used.", "",
        "## Manual-only boundary", "", "Manual review required; no order can be placed by this tool.", "",
        "## Embedded payload", "", "```json", json.dumps(payload,ensure_ascii=False,sort_keys=True,allow_nan=False), "```", "",
    ])+"\n"
    validate_report_safety(text)
    return text

def validate_phase3_report(report: str) -> None:
    validate_report_safety(report)
    if "Manual-only boundary" not in report: raise ValueError("Phase 3 report is missing the manual-only boundary")
