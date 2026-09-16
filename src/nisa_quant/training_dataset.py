"""Monthly point-in-time panel construction and artifact identity."""
from __future__ import annotations
import hashlib, json, math
from dataclasses import dataclass, asdict
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from .historical_market_data import (
    HistorySnapshot,
    MEMBERSHIP_EVIDENCE_STATUSES,
    has_point_in_time_membership_evidence,
    is_usable_market_bar,
    validate_universe_member,
)
from .feature_engineering import FEATURE_SCHEMA, build_features, usable_market_observation_count
from .return_targets import calculate_forward_targets, validate_target_contract

@dataclass(frozen=True, slots=True)
class PanelRow:
    ticker: str
    decision_date: str
    features: dict[str, float | None]
    targets: dict[str, Any]
    target_metadata: dict[str, Any] | None = None

@dataclass(frozen=True, slots=True)
class TrainingDataset:
    rows: list[PanelRow]
    dataset_id: str
    history_snapshot_id: str
    benchmark_ticker: str
    feature_schema: tuple[str, ...] = FEATURE_SCHEMA
    return_basis: str = "price_return"
    sec_source_ids: tuple[str, ...] = ()
    sec_context_ids: tuple[str, ...] = ()
    membership_evidence_status: str = "point_in_time_membership_evidence"
    schema_version: int = 1
    target_horizon: str = "3m"


DATASET_SCHEMA_VERSION = 1


def _validate_finite_tree(value: Any, *, path: str) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"dataset {path} must contain finite numbers")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"dataset {path} has a non-string key")
            _validate_finite_tree(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_finite_tree(item, path=f"{path}[{index}]")


def _validate_dataset(dataset: TrainingDataset) -> None:
    if dataset.schema_version != DATASET_SCHEMA_VERSION:
        raise ValueError("dataset schema version is unsupported")
    if not all(isinstance(value, str) and value for value in (
        dataset.dataset_id, dataset.history_snapshot_id, dataset.benchmark_ticker,
        dataset.return_basis, dataset.membership_evidence_status,
    )):
        raise ValueError("dataset identity and binding fields must be non-empty strings")
    if dataset.membership_evidence_status not in MEMBERSHIP_EVIDENCE_STATUSES:
        raise ValueError("dataset membership evidence status is unsupported")
    if dataset.return_basis != "price_return":
        raise ValueError("dataset return basis must be price_return")
    if dataset.target_horizon not in {"3m", "6m", "12m"}:
        raise ValueError("dataset target horizon is unsupported")
    if not isinstance(dataset.feature_schema, tuple) or not dataset.feature_schema or any(
        not isinstance(value, str) or not value for value in dataset.feature_schema
    ):
        raise ValueError("dataset feature schema is invalid")
    for name, values in (("sec_source_ids", dataset.sec_source_ids), ("sec_context_ids", dataset.sec_context_ids)):
        if not isinstance(values, tuple) or any(not isinstance(value, str) or not value for value in values):
            raise ValueError(f"dataset {name} is invalid")
    for index, row in enumerate(dataset.rows):
        if not isinstance(row, PanelRow):
            raise ValueError(f"dataset row {index} is not a PanelRow")
        if not isinstance(row.ticker, str) or not row.ticker or not isinstance(row.decision_date, str):
            raise ValueError(f"dataset row {index} identity is invalid")
        try:
            date.fromisoformat(row.decision_date)
        except ValueError as exc:
            raise ValueError(f"dataset row {index} decision_date is invalid") from exc
        if set(row.features) != set(dataset.feature_schema):
            raise ValueError(f"dataset row {index} feature schema is invalid")
        if not isinstance(row.targets, dict):
            raise ValueError(f"dataset row {index} targets must be an object")
        if row.target_metadata is not None and not isinstance(row.target_metadata, dict):
            raise ValueError(f"dataset row {index} target_metadata must be an object or null")
        _validate_finite_tree(row.features, path=f"rows[{index}].features")
        _validate_finite_tree(row.targets, path=f"rows[{index}].targets")
        _validate_finite_tree(row.target_metadata, path=f"rows[{index}].target_metadata")
        validate_target_contract(row)

def _identity(rows: Sequence[PanelRow], history_snapshot_id: str, benchmark_ticker: str, schema: Sequence[str], basis: str, sec_source_ids: Sequence[str] = (), sec_context_ids: Sequence[str] = (), membership_evidence_status: str = "point_in_time_membership_evidence", target_horizon: str = "3m") -> str:
    payload = {"history_snapshot_id":history_snapshot_id,"benchmark_ticker":benchmark_ticker,"feature_schema":list(schema),"return_basis":basis,"target_horizon":target_horizon,"sec_source_ids":sorted(sec_source_ids),"sec_context_ids":sorted(sec_context_ids),"membership_evidence_status":membership_evidence_status,"rows":[asdict(r) for r in rows]}
    return "dataset-" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",",":"), allow_nan=False).encode()).hexdigest()[:20]

def build_monthly_panel(
    snapshot: HistorySnapshot, *, as_of: date | str | None = None,
    decision_dates: Sequence[date | str] | None = None, target_horizon: str = "3m",
) -> TrainingDataset:
    if target_horizon not in {"3m", "6m", "12m"}:
        raise ValueError("target horizon is unsupported")
    universe = [validate_universe_member(member) for member in snapshot.universe]
    cutoff = date.fromisoformat(as_of) if isinstance(as_of,str) else as_of
    all_bars = {ticker: bars for ticker,bars in snapshot.bars_by_ticker.items() if ticker != snapshot.benchmark_ticker}
    if cutoff: all_dates = [date.fromisoformat(b.observation_date) for b in snapshot.bars_by_ticker.get(snapshot.benchmark_ticker, []) if date.fromisoformat(b.observation_date)<=cutoff and is_usable_market_bar(b)]
    else: all_dates = [date.fromisoformat(b.observation_date) for b in snapshot.bars_by_ticker.get(snapshot.benchmark_ticker, []) if is_usable_market_bar(b)]
    if decision_dates is None:
        dates=[]; seen=set()
        for value in all_dates:
            key=(value.year,value.month)
            if key not in seen: dates.append(value); seen.add(key)
        decision_dates=dates
    decisions=sorted({d if isinstance(d,date) else date.fromisoformat(d) for d in decision_dates})
    if cutoff is not None and any(decision > cutoff for decision in decisions):
        raise ValueError("explicit decision date is after the as_of cutoff")
    rows=[]
    sec_source_ids: set[str] = set()
    sec_context_ids: set[str] = set()
    for index, decision in enumerate(decisions):
        next_decision = decisions[index+1] if index+1<len(decisions) else None
        for ticker,bars in all_bars.items():
            if ticker in snapshot.ticker_failures:
                continue
            membership = next((u for u in universe if u.ticker == ticker), None)
            if membership and membership.effective_from and decision < date.fromisoformat(membership.effective_from): continue
            if membership and membership.effective_to and decision > date.fromisoformat(membership.effective_to): continue
            market_observation_count = usable_market_observation_count(bars, decision)
            if market_observation_count == 0:
                continue
            feature = build_features(ticker,bars,decision_date=decision,benchmark_bars=snapshot.bars_by_ticker.get(snapshot.benchmark_ticker,[]),sec_facts=snapshot.sec_facts)
            sec_source_ids.update(feature.sec_source_ids)
            sec_context_ids.update(feature.sec_context_ids)
            targets=calculate_forward_targets({ticker:bars}, snapshot.bars_by_ticker.get(snapshot.benchmark_ticker,[]), decision, next_decision_date=next_decision, return_basis="price_return", monthly_interval=True)
            metadata={key:value for key,value in targets.items() if "interval" in key or "availability" in key or "basis" in key or "endpoint" in key}
            if feature.sec_source_ids:
                metadata["sec_source_ids"] = list(feature.sec_source_ids)
            if feature.sec_context_ids:
                metadata["sec_context_ids"] = list(feature.sec_context_ids)
            latest_market_date = max(
                date.fromisoformat(bar.observation_date)
                for bar in bars
                if is_usable_market_bar(bar) and date.fromisoformat(bar.observation_date) <= decision
            )
            metadata["market_observation_count"] = market_observation_count
            metadata["market_observation_date"] = latest_market_date.isoformat()
            metadata["market_staleness_days"] = (decision - latest_market_date).days
            if membership and membership.lookahead_bias_status == "current_snapshot_only": metadata["current_snapshot_only"]=True
            rows.append(PanelRow(ticker,decision.isoformat(),feature.values,targets,metadata))
    point_in_time = bool(universe) and all(
        has_point_in_time_membership_evidence(member) for member in universe
    )
    membership_evidence_status = "point_in_time_membership_evidence" if point_in_time else "descriptive_survivor_selected_evidence"
    dataset_id=_identity(rows,snapshot.snapshot_id,snapshot.benchmark_ticker,FEATURE_SCHEMA,"price_return",sec_source_ids,sec_context_ids,membership_evidence_status,target_horizon)
    dataset = TrainingDataset(rows,dataset_id,snapshot.snapshot_id,snapshot.benchmark_ticker,FEATURE_SCHEMA,"price_return",tuple(sorted(sec_source_ids)),tuple(sorted(sec_context_ids)),membership_evidence_status,1,target_horizon)
    _validate_dataset(dataset)
    return dataset

def save_training_dataset(dataset: TrainingDataset, path) -> None:
    _validate_dataset(dataset)
    expected = _identity(dataset.rows, dataset.history_snapshot_id, dataset.benchmark_ticker,
                         dataset.feature_schema, dataset.return_basis, dataset.sec_source_ids,
                         dataset.sec_context_ids, dataset.membership_evidence_status, dataset.target_horizon)
    if dataset.dataset_id != expected:
        raise ValueError("training dataset content/hash does not match dataset identity")
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(dataset),sort_keys=True,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    temporary.replace(path)

def load_training_dataset(path) -> TrainingDataset:
    payload=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("training dataset must be a JSON object")
    expected_fields = set(TrainingDataset.__dataclass_fields__)
    if set(payload) != expected_fields:
        raise ValueError("training dataset has an invalid field set")
    if payload.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise ValueError("dataset schema version is unsupported")
    raw_rows = payload["rows"]
    if not isinstance(raw_rows, list):
        raise ValueError("training dataset rows must be a list")
    rows=[]
    for index, row in enumerate(raw_rows):
        if not isinstance(row, dict) or set(row) != set(PanelRow.__dataclass_fields__):
            raise ValueError(f"training dataset row {index} has an invalid field set")
        rows.append(PanelRow(**row))
    dataset=TrainingDataset(
        rows, payload["dataset_id"], payload["history_snapshot_id"], payload["benchmark_ticker"],
        tuple(payload["feature_schema"]), payload["return_basis"], tuple(payload["sec_source_ids"]),
        tuple(payload["sec_context_ids"]), payload["membership_evidence_status"], payload["schema_version"], payload["target_horizon"],
    )
    _validate_dataset(dataset)
    if dataset.dataset_id != _identity(rows,dataset.history_snapshot_id,dataset.benchmark_ticker,dataset.feature_schema,dataset.return_basis,dataset.sec_source_ids,dataset.sec_context_ids,dataset.membership_evidence_status,dataset.target_horizon): raise ValueError("training dataset content/hash does not match dataset identity")
    return dataset
