"""Deterministic ridge ranking model and transparent factor score."""
from __future__ import annotations
import hashlib, json, math
from dataclasses import dataclass, asdict
from datetime import date
from typing import Any, Iterable, Mapping, Sequence
from .feature_engineering import FEATURE_SCHEMA
from .return_targets import target_label_available_by, validate_target_contract

MODEL_VERSION="phase3-ridge-ranking-v2"
MISSING_FEATURE_POLICY = "training_mean_per_feature_with_zero_fallback_v1"
# A monthly decision row can legitimately be based on the last observation at
# that decision date.  Allow one calendar month for publication/market
# cadence, while retaining a finite bound against genuinely stale history.
CURRENT_PREDICTION_FRESHNESS_POLICY = "monthly_decision_date_31_calendar_day_allowance_v1"
CURRENT_PREDICTION_MAX_STALENESS_DAYS = 31


def _is_typed_panel_row(row: Any) -> bool:
    from .training_dataset import PanelRow
    return isinstance(row, PanelRow)


def _validate_training_window(value: object, *, label: str = "training window") -> int | None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
    ):
        raise ValueError(f"{label} must be a positive integer or null")
    return value


def _count_usable_training_rows(
    rows: Sequence[Any], *, training_cutoff: date | str,
    target_horizon: str = "3m", training_window: int | None = None,
    target_observable_by: date | str | None = None,
) -> int:
    cutoff = training_cutoff if isinstance(training_cutoff, date) else date.fromisoformat(training_cutoff)
    observable = cutoff if target_observable_by is None else (
        target_observable_by if isinstance(target_observable_by, date)
        else date.fromisoformat(target_observable_by)
    )
    usable: list[Any] = []
    for row in rows:
        if date.fromisoformat(row.decision_date) >= cutoff:
            continue
        if observable and not target_label_available_by(row, observable, target_horizon):
            continue
        target = row.targets.get(f"target_{target_horizon}_excess_return")
        if target is None:
            continue
        try:
            target_value = float(target)
        except (TypeError, ValueError):
            continue
        if math.isfinite(target_value):
            usable.append(row)
    if training_window is None:
        return len(usable)
    eligible_dates = sorted({date.fromisoformat(row.decision_date) for row in usable})
    selected_dates = set(eligible_dates[-training_window:])
    return sum(date.fromisoformat(row.decision_date) in selected_dates for row in usable)


def current_prediction_freshness(row: Any, as_of: date | str) -> dict[str, Any]:
    """Return deterministic freshness evidence for a current market row."""
    cutoff = as_of if isinstance(as_of, date) else date.fromisoformat(as_of)
    if isinstance(row, Mapping):
        get_value = lambda key, default=None: row.get(key, default)
    else:
        get_value = lambda key, default=None: getattr(row, key, default)
    metadata = get_value("target_metadata", None)
    observation_raw = metadata.get("market_observation_date") if isinstance(metadata, Mapping) else None
    evidence_base = {
        "ticker": get_value("ticker", None),
        "decision_date": get_value("decision_date", None),
        "as_of": cutoff.isoformat(),
        "threshold_days": CURRENT_PREDICTION_MAX_STALENESS_DAYS,
        "freshness_policy": CURRENT_PREDICTION_FRESHNESS_POLICY,
    }
    if observation_raw is None:
        return {
            **evidence_base,
            "status": "unavailable",
            "market_observation_date": None,
            "evidence_source": "target_metadata.market_observation_date",
            "reason": "market observation date is missing; feature staleness cannot establish absolute freshness",
        }
    try:
        observation = date.fromisoformat(observation_raw)
    except (TypeError, ValueError):
        return {
            **evidence_base,
            "status": "unavailable",
            "market_observation_date": None,
            "evidence_source": "target_metadata.market_observation_date",
            "reason": "market observation date is malformed",
        }
    if observation > cutoff:
        return {
            **evidence_base,
            "market_observation_date": observation.isoformat(),
            "status": "unavailable",
            "evidence_source": "market_observation_date",
            "reason": "market observation date is after as_of",
        }
    staleness_days = float((cutoff - observation).days)
    evidence_source = "market_observation_date"
    status = "fresh" if staleness_days <= CURRENT_PREDICTION_MAX_STALENESS_DAYS else "stale"
    return {
        "ticker": get_value("ticker", None),
        "decision_date": get_value("decision_date", None),
        "status": status,
        "staleness_days": staleness_days,
        "threshold_days": CURRENT_PREDICTION_MAX_STALENESS_DAYS,
        "evidence_source": evidence_source,
        "as_of": cutoff.isoformat(),
        "market_observation_date": observation.isoformat() if observation_raw is not None else None,
        "freshness_policy": CURRENT_PREDICTION_FRESHNESS_POLICY,
    }

@dataclass(frozen=True, slots=True)
class RankingModelArtifact:
    model_version: str
    feature_schema: tuple[str,...]
    training_cutoff: str
    dataset_id: str
    history_snapshot_id: str
    coefficients: tuple[float,...]
    intercept: float
    means: tuple[float,...]
    scales: tuple[float,...]
    residual_std: float
    training_rows: int
    target_horizon: str = "3m"
    return_basis: str = "price_return"
    benchmark_ticker: str = ""
    training_window: int | None = None
    schema_version: int = 1
    model_id: str = ""
    content_hash: str = ""
    imputation_values: tuple[float, ...] = ()
    missing_feature_policy: str = MISSING_FEATURE_POLICY

    def predict_row(self,row: Any)->float|None:
        if len(self.imputation_values) != len(self.feature_schema):
            return None
        values=[]
        for index, name in enumerate(self.feature_schema):
            value = row.features.get(name)
            if value is None:
                value = self.imputation_values[index]
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value):
                return None
            values.append(value)
        return self.intercept+sum(coef*((float(value)-mean)/scale) for coef,value,mean,scale in zip(self.coefficients,values,self.means,self.scales))


def _artifact_payload(artifact: RankingModelArtifact) -> dict[str, Any]:
    payload = asdict(artifact)
    payload.pop("model_id", None)
    payload.pop("content_hash", None)
    return payload


def _artifact_hash(artifact: RankingModelArtifact) -> str:
    return hashlib.sha256(json.dumps(_artifact_payload(artifact), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _with_identity(artifact: RankingModelArtifact) -> RankingModelArtifact:
    digest = _artifact_hash(artifact)
    return RankingModelArtifact(**{**asdict(artifact), "model_id": f"model-{digest[:20]}", "content_hash": digest})


def _validate_artifact(artifact: RankingModelArtifact) -> None:
    if artifact.schema_version != 1 or artifact.return_basis != "price_return":
        raise ValueError("model artifact schema or return basis is unsupported")
    if not all(isinstance(value, str) and value for value in (artifact.model_version, artifact.training_cutoff, artifact.target_horizon, artifact.return_basis, artifact.model_id, artifact.content_hash)) or any(not isinstance(value, str) for value in (artifact.dataset_id, artifact.history_snapshot_id, artifact.benchmark_ticker)):
        raise ValueError("model artifact binding fields are invalid")
    try:
        date.fromisoformat(artifact.training_cutoff)
    except ValueError as exc:
        raise ValueError("model artifact training cutoff is invalid") from exc
    if artifact.target_horizon not in {"3m", "6m", "12m"} or not isinstance(artifact.feature_schema, tuple) or not artifact.feature_schema:
        raise ValueError("model artifact target or feature schema is invalid")
    if len(artifact.coefficients) != len(artifact.feature_schema) or len(artifact.means) != len(artifact.feature_schema) or len(artifact.scales) != len(artifact.feature_schema):
        raise ValueError("model artifact vector lengths do not match feature schema")
    if artifact.missing_feature_policy != MISSING_FEATURE_POLICY or len(artifact.imputation_values) != len(artifact.feature_schema):
        raise ValueError("model artifact missing-feature policy is invalid")
    values = (*artifact.coefficients, artifact.intercept, *artifact.means, *artifact.scales, *artifact.imputation_values, artifact.residual_std)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in values):
        raise ValueError("model artifact contains non-finite values")
    _validate_training_window(artifact.training_window)
    if any(scale == 0 for scale in artifact.scales) or isinstance(artifact.training_rows, bool) or not isinstance(artifact.training_rows, int) or artifact.training_rows <= 0:
        raise ValueError("model artifact training statistics are invalid")
    expected_hash = _artifact_hash(artifact)
    if artifact.content_hash != expected_hash or artifact.model_id != f"model-{expected_hash[:20]}":
        raise ValueError("model artifact content/hash does not match model identity")

class SpecializedRankingModel:
    def __init__(self, artifact: RankingModelArtifact|None=None): self.artifact=artifact

    @staticmethod
    def _endpoint(row: Any, horizon: str="3m") -> date:
        explicit=row.targets.get(f"target_{horizon}_forward_endpoint")
        if not isinstance(explicit, str):
            raise ValueError(f"target_{horizon} forward endpoint is required")
        endpoint=date.fromisoformat(explicit)
        interval_end=row.targets.get(f"target_{horizon}_interval_end")
        if interval_end is not None and interval_end != explicit:
            raise ValueError(f"target_{horizon} interval end does not match its forward endpoint")
        return endpoint

    def fit(self, rows: Sequence[Any], *, training_cutoff: date|str, dataset_id: str="", history_snapshot_id: str="", benchmark_ticker: str="", target_horizon: str="3m", training_window: int|None=None, target_observable_by: date|str|None=None) -> RankingModelArtifact:
        cutoff=training_cutoff if isinstance(training_cutoff,date) else date.fromisoformat(training_cutoff)
        if target_horizon not in {"3m", "6m", "12m"}:
            raise ValueError("model target horizon is unsupported")
        _validate_training_window(training_window, label="model training window")
        # A direct fit must always use an exclusive information cutoff.  If the
        # caller does not provide a separate observability date, the training
        # cutoff is the only safe derivation.
        observable = cutoff if target_observable_by is None else (target_observable_by if isinstance(target_observable_by,date) else date.fromisoformat(target_observable_by))
        usable=[]
        for row in rows:
            if _is_typed_panel_row(row):
                validate_target_contract(row)
            if date.fromisoformat(row.decision_date)>=cutoff: continue
            if observable and not target_label_available_by(row, observable, target_horizon): continue
            target=row.targets.get(f"target_{target_horizon}_excess_return")
            vals=[row.features.get(name) for name in FEATURE_SCHEMA]
            if target is None:
                continue
            try:
                target_value = float(target)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(target_value):
                continue
            usable_values=[]
            for value in vals:
                if value is None:
                    usable_values.append(None)
                    continue
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    usable_values.append(None)
                    continue
                usable_values.append(numeric if math.isfinite(numeric) else None)
            usable.append((row, usable_values, target_value))
        if training_window is not None:
            eligible_dates = sorted({date.fromisoformat(row.decision_date) for row, _, _ in usable})
            selected_dates = set(eligible_dates[-training_window:])
            usable = [item for item in usable if date.fromisoformat(item[0].decision_date) in selected_dates]
        if not usable: raise ValueError("no observable finite training rows")
        imputation_values=[]
        for index in range(len(FEATURE_SCHEMA)):
            observed=[values[index] for _, values, _ in usable if values[index] is not None]
            imputation_values.append(sum(observed)/len(observed) if observed else 0.0)
        matrix=[[value if value is not None else imputation_values[index] for index, value in enumerate(values)] for _, values, _ in usable]
        ys=[x[2] for x in usable]; means=[sum(x[i] for x in matrix)/len(matrix) for i in range(len(FEATURE_SCHEMA))]
        scales=[math.sqrt(sum((x[i]-means[i])**2 for x in matrix)/len(matrix)) or 1.0 for i in range(len(FEATURE_SCHEMA))]
        # Small ridge-like diagonal fit, deterministic without external dependencies.
        z=[[ (x[i]-means[i])/scales[i] for i in range(len(FEATURE_SCHEMA))] for x in matrix]
        ymean=sum(ys)/len(ys); coefs=[]
        for i in range(len(FEATURE_SCHEMA)):
            den=sum(row[i]*row[i] for row in z)+1.0
            coefs.append(sum(row[i]*(y-ymean) for row,y in zip(z,ys))/den)
        predictions=[ymean+sum(c*v for c,v in zip(coefs,row)) for row in z]
        residual=math.sqrt(sum((a-b)**2 for a,b in zip(ys,predictions))/len(ys)) if ys else 0.0
        artifact=RankingModelArtifact(
            model_version=MODEL_VERSION, feature_schema=FEATURE_SCHEMA,
            training_cutoff=cutoff.isoformat(), dataset_id=dataset_id,
            history_snapshot_id=history_snapshot_id, coefficients=tuple(coefs),
            intercept=ymean, means=tuple(means), scales=tuple(scales),
            residual_std=residual, training_rows=len(usable),
            target_horizon=target_horizon, return_basis="price_return",
            benchmark_ticker=benchmark_ticker, training_window=training_window,
            imputation_values=tuple(imputation_values),
        )
        artifact = _with_identity(artifact)
        self.artifact=artifact; return artifact
    def predict(self,row: Any)->float|None:
        if not self.artifact: raise ValueError("model is not fitted")
        return self.artifact.predict_row(row)


def rank_current_candidates(
    dataset: Any, artifact: RankingModelArtifact, *, as_of: date | str,
) -> list[dict[str, Any]]:
    """Rank the latest eligible rows as descriptive model output only."""
    if not isinstance(artifact, RankingModelArtifact):
        raise ValueError("current ranking requires a fitted model artifact")
    _validate_artifact(artifact)
    expected_training_rows = _count_usable_training_rows(
        dataset.rows, training_cutoff=artifact.training_cutoff,
        target_horizon=artifact.target_horizon, training_window=artifact.training_window,
    )
    if artifact.training_rows != expected_training_rows:
        raise ValueError("current ranking model training-row provenance is invalid")
    if artifact.target_horizon != "3m":
        raise ValueError("current ranking requires a 3m model artifact")
    if getattr(dataset, "return_basis", "price_return") != "price_return":
        raise ValueError("current ranking requires a price_return dataset")
    if any((
        artifact.dataset_id != getattr(dataset, "dataset_id", ""),
        artifact.history_snapshot_id != getattr(dataset, "history_snapshot_id", ""),
        artifact.benchmark_ticker != getattr(dataset, "benchmark_ticker", ""),
        artifact.target_horizon != getattr(dataset, "target_horizon", "3m"),
        artifact.feature_schema != tuple(getattr(dataset, "feature_schema", ())),
    )):
        raise ValueError("current ranking artifact does not match dataset")
    cutoff = as_of if isinstance(as_of, date) else date.fromisoformat(as_of)
    eligible = []
    for row in getattr(dataset, "rows", ()):
        if _is_typed_panel_row(row):
            validate_target_contract(row)
        try:
            decision = date.fromisoformat(row.decision_date)
        except (AttributeError, TypeError, ValueError):
            continue
        if decision <= cutoff and isinstance(getattr(row, "ticker", None), str) and row.ticker and isinstance(getattr(row, "features", None), Mapping):
            eligible.append((decision, row))
    if not eligible:
        return []
    latest_date = max(decision for decision, _ in eligible)
    current_only = (
        getattr(dataset, "membership_evidence_status", "") == "descriptive_survivor_selected_evidence"
        or any(
            isinstance(getattr(row, "target_metadata", None), Mapping)
            and row.target_metadata.get("current_snapshot_only") is True
            for _, row in eligible if date.fromisoformat(row.decision_date) == latest_date
        )
    )
    evidence_status = "descriptive_survivor_selected_evidence" if current_only else "point_in_time_membership_evidence"
    ranked: list[tuple[float, Any]] = []
    for decision, row in eligible:
        if decision != latest_date:
            continue
        metadata = getattr(row, "target_metadata", None)
        if isinstance(metadata, Mapping) and "market_observation_count" in metadata:
            count = metadata["market_observation_count"]
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                continue
        if current_prediction_freshness(row, cutoff).get("status") != "fresh":
            continue
        try:
            score = artifact.predict_row(row)
        except (TypeError, ValueError, OverflowError):
            continue
        if score is not None and math.isfinite(float(score)):
            ranked.append((float(score), row))
    ranked.sort(key=lambda item: (-item[0], item[1].ticker))
    return [
        {
            "ticker": row.ticker,
            "rank": rank,
            "model_score": score,
            "predicted_3m_excess_return": score,
            "target_horizon": artifact.target_horizon,
            "model_id": artifact.model_id,
            "dataset_id": artifact.dataset_id,
            "history_snapshot_id": artifact.history_snapshot_id,
            "benchmark": artifact.benchmark_ticker,
            "return_basis": artifact.return_basis,
            "decision_date": latest_date.isoformat(),
            "evidence_status": evidence_status,
            "freshness_evidence": current_prediction_freshness(row, cutoff),
        }
        for rank, (score, row) in enumerate(ranked, start=1)
    ]

def factor_score(row: Any) -> float|None:
    weights={"momentum_1m":.2,"momentum_3m":.3,"momentum_6m":.2,"momentum_12m":.1,"relative_strength":.15,"volatility":-.025,"drawdown":-.025}
    values=[]
    for name,weight in weights.items():
        value=row.features.get(name)
        if value is None or not math.isfinite(float(value)): return None
        values.append(weight*float(value))
    return sum(values)

def save_model(artifact: RankingModelArtifact,path) -> None:
    _validate_artifact(artifact)
    expected = _with_identity(RankingModelArtifact(**{**asdict(artifact), "model_id": "", "content_hash": ""}))
    if artifact.model_id != expected.model_id or artifact.content_hash != expected.content_hash:
        raise ValueError("model artifact content/hash does not match model identity")
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(artifact),sort_keys=True,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    temporary.replace(path)

def load_model(path) -> RankingModelArtifact:
    payload=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != set(RankingModelArtifact.__dataclass_fields__):
        raise ValueError("model artifact has an invalid field set")
    artifact = RankingModelArtifact(
        model_version=payload["model_version"], feature_schema=tuple(payload["feature_schema"]),
        training_cutoff=payload["training_cutoff"], dataset_id=payload["dataset_id"],
        history_snapshot_id=payload["history_snapshot_id"], coefficients=tuple(payload["coefficients"]),
        intercept=payload["intercept"], means=tuple(payload["means"]), scales=tuple(payload["scales"]),
        residual_std=payload["residual_std"], training_rows=payload["training_rows"],
        target_horizon=payload["target_horizon"], return_basis=payload["return_basis"],
        benchmark_ticker=payload["benchmark_ticker"], training_window=payload["training_window"],
        schema_version=payload["schema_version"], model_id=payload["model_id"],
        content_hash=payload["content_hash"], imputation_values=tuple(payload["imputation_values"]),
        missing_feature_policy=payload["missing_feature_policy"],
    )
    _validate_artifact(artifact)
    expected = _with_identity(RankingModelArtifact(**{**asdict(artifact), "model_id": "", "content_hash": ""}))
    if artifact.model_id != expected.model_id or artifact.content_hash != expected.content_hash:
        raise ValueError("model artifact content/hash does not match model identity")
    return artifact
