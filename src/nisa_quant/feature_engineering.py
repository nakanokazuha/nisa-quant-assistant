"""Point-in-time, dependency-free tabular features for Phase 3."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Sequence

from .historical_market_data import (
    MarketBar,
    SecFact,
    SUPPORTED_SEC_CONCEPTS,
    SUPPORTED_SEC_FORMS,
    SUPPORTED_SEC_UNITS,
    _validate_sec_fact,
    is_usable_market_bar,
)

FEATURE_SCHEMA = (
    "momentum_1m", "momentum_3m", "momentum_6m", "momentum_12m", "relative_strength",
    "volatility", "drawdown", "dollar_volume", "volume_trend", "revenue", "revenue_growth",
    "net_margin", "filing_count", "missingness", "staleness",
)

@dataclass(frozen=True, slots=True)
class FeatureResult:
    ticker: str
    decision_date: str
    values: dict[str, float | None]
    feature_schema: tuple[str, ...] = FEATURE_SCHEMA
    return_basis: str = "price_return"
    source_snapshot_ids: tuple[str, ...] = ()
    sec_source_ids: tuple[str, ...] = ()
    sec_context_ids: tuple[str, ...] = ()

def _finite(x: object) -> float | None:
    try:
        y = float(x)
    except (TypeError, ValueError): return None
    return y if math.isfinite(y) else None

def _at_or_before(bars: Sequence[MarketBar], cutoff: date) -> list[MarketBar]:
    return [b for b in bars if date.fromisoformat(b.observation_date) <= cutoff and is_usable_market_bar(b)]


def usable_market_observation_count(bars: Sequence[MarketBar], cutoff: date) -> int:
    """Count finite close observations available at a decision cutoff."""
    return len(_at_or_before(bars, cutoff))

def _return(values: Sequence[float], periods: int) -> float | None:
    if len(values) <= periods or values[-periods-1] <= 0: return None
    return values[-1] / values[-periods-1] - 1.0

def _validate_sec_feature_fact(fact: SecFact) -> None:
    if not isinstance(fact, SecFact):
        raise ValueError("SEC feature facts must be typed SecFact objects")
    if fact.concept not in SUPPORTED_SEC_CONCEPTS:
        raise ValueError(f"unsupported SEC concept: {fact.concept}")
    if fact.unit not in SUPPORTED_SEC_UNITS:
        raise ValueError(f"unsupported SEC unit: {fact.unit}")
    if fact.form not in SUPPORTED_SEC_FORMS:
        raise ValueError(f"unsupported SEC form: {fact.form}")
    _validate_sec_fact(fact)
    try:
        date.fromisoformat(fact.period_end)
        date.fromisoformat(fact.filed_at)
        if fact.period_start is not None:
            date.fromisoformat(fact.period_start)
    except ValueError as exc:
        raise ValueError("SEC feature fact has invalid fiscal dates") from exc


def _form_family(form: str) -> str:
    return form.removesuffix("/A")


def _same_fiscal_context(left: SecFact, right: SecFact) -> bool:
    if left.concept != right.concept or left.unit != right.unit or _form_family(left.form) != _form_family(right.form):
        return False
    if (left.period_start is None) != (right.period_start is None):
        return False
    if left.period_start and right.period_start:
        left_duration = date.fromisoformat(left.period_end) - date.fromisoformat(left.period_start)
        right_duration = date.fromisoformat(right.period_end) - date.fromisoformat(right.period_start)
        if left_duration != right_duration:
            return False
    if left.fiscal_period != right.fiscal_period:
        return False
    return True


def _same_margin_context(revenue: SecFact, income: SecFact) -> bool:
    return (
        income.unit == revenue.unit
        and _form_family(income.form) == _form_family(revenue.form)
        and income.period_start == revenue.period_start
        and income.period_end == revenue.period_end
        and income.fiscal_period == revenue.fiscal_period
    )


def _fact_source_id(fact: SecFact) -> str:
    return fact.citation or f"{fact.source}:{fact.accession}:{fact.filed_at}"


def _fact_context_id(fact: SecFact) -> str:
    return json.dumps({
        "concept": fact.concept,
        "unit": fact.unit,
        "form": fact.form,
        "period_start": fact.period_start,
        "period_end": fact.period_end,
        "fiscal_year": fact.fiscal_year,
        "fiscal_period": fact.fiscal_period,
        "frame": fact.frame,
        "accession": fact.accession,
        "source": fact.source,
        "citation": fact.citation,
        "retrieved_at": fact.retrieved_at,
    }, sort_keys=True, separators=(",", ":"))


def _latest_facts(facts: Iterable[SecFact], ticker: str, cutoff: date) -> dict[str, SecFact]:
    selected = [f for f in facts if f.ticker == ticker and date.fromisoformat(f.filed_at) <= cutoff]
    revenues = [f for f in selected if f.concept in {"us-gaap:Revenues", "us-gaap:SalesRevenueNet", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"}]
    revenues.sort(key=lambda f: (f.period_end, f.filed_at))
    latest = revenues[-1] if revenues else None
    result: dict[str, SecFact] = {}
    if latest:
        result["revenue"] = latest
        comparable = [f for f in revenues if _same_fiscal_context(latest, f)]
        prior = [f for f in comparable if f.period_end < latest.period_end]
        if prior: result["prior_revenue"] = prior[-1]
        incomes = [f for f in selected if f.concept in {"us-gaap:NetIncomeLoss", "us-gaap:ProfitLoss"} and _same_margin_context(latest, f)]
        if incomes: result["income"] = sorted(incomes, key=lambda f: f.filed_at)[-1]
    return result

def build_features(ticker: str, bars: Sequence[MarketBar], *, decision_date: date | str, benchmark_bars: Sequence[MarketBar] = (), sec_facts: Iterable[SecFact] = ()) -> FeatureResult:
    cutoff = decision_date if isinstance(decision_date, date) else date.fromisoformat(decision_date)
    usable = _at_or_before(bars, cutoff)
    benchmark = _at_or_before(benchmark_bars, cutoff)
    prices = [float(b.close) for b in usable]
    benchmark_prices = [float(b.close) for b in benchmark]
    values: dict[str, float | None] = {}
    for name, periods in (("momentum_1m",21),("momentum_3m",63),("momentum_6m",126),("momentum_12m",252)):
        values[name] = _return(prices, periods)
    values["relative_strength"] = (values["momentum_3m"] - _return(benchmark_prices,63) if values["momentum_3m"] is not None and _return(benchmark_prices,63) is not None else None)
    returns = [prices[i]/prices[i-1]-1 for i in range(1,len(prices)) if prices[i-1] > 0]
    values["volatility"] = math.sqrt(252.0) * math.sqrt(sum((r-(sum(returns)/len(returns)))**2 for r in returns)/(len(returns)-1)) if len(returns)>1 else None
    peak = max(prices) if prices else None
    values["drawdown"] = prices[-1]/peak-1 if peak and prices else None
    values["dollar_volume"] = float(usable[-1].close) * float(usable[-1].volume) if usable else None
    volumes = [float(b.volume) for b in usable if b.volume is not None]
    if not volumes:
        values["volume_trend"] = None
    else:
        rolling_mean = sum(volumes[-21:]) / min(21, len(volumes[-21:]))
        values["volume_trend"] = 0.0 if rolling_mean == 0.0 else volumes[-1] / rolling_mean - 1.0
    typed_sec_facts = list(sec_facts)
    for fact in typed_sec_facts:
        _validate_sec_feature_fact(fact)
    selected_sec_facts = [fact for fact in typed_sec_facts if fact.ticker == ticker and date.fromisoformat(fact.filed_at) <= cutoff]
    facts = _latest_facts(selected_sec_facts, ticker, cutoff)
    values["revenue"] = facts.get("revenue").value if facts.get("revenue") else None
    values["revenue_growth"] = (facts["revenue"].value/facts["prior_revenue"].value-1 if "revenue" in facts and "prior_revenue" in facts and facts["prior_revenue"].value else None)
    values["net_margin"] = (facts["income"].value/facts["revenue"].value if "income" in facts and facts["revenue"].value else None)
    values["filing_count"] = float(len(selected_sec_facts))
    values["missingness"] = sum(value is None for value in values.values()) / len(FEATURE_SCHEMA)
    values["staleness"] = (cutoff - date.fromisoformat(usable[-1].observation_date)).days if usable else None
    sec_source_ids = tuple(dict.fromkeys(_fact_source_id(fact) for fact in selected_sec_facts))
    sec_context_ids = tuple(dict.fromkeys(_fact_context_id(fact) for fact in selected_sec_facts))
    return FeatureResult(ticker, cutoff.isoformat(), values, FEATURE_SCHEMA, "price_return", (), sec_source_ids, sec_context_ids)
