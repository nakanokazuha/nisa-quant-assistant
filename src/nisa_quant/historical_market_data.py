"""Bounded, read-only historical market and SEC Company Facts adapters."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from numbers import Real
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .evidence_providers import HttpResponse, ProviderUnavailable, UrllibReadOnlyTransport
from .source_records import parse_retrieved_at

YAHOO_HOSTS = frozenset({"query1.finance.yahoo.com", "query2.finance.yahoo.com"})
SEC_HOSTS = frozenset({"data.sec.gov", "www.sec.gov"})
SEC_COMPANY_FACTS_BASE_URL = "https://data.sec.gov/api/xbrl/companyfacts/"
SEC_COMPANY_FACTS_PROVIDER = "SEC XBRL Company Facts"
SEC_COMPANY_FACTS_SOURCE_VERSIONS = frozenset({"companyfacts-v1", "legacy-fixture-v1"})
SEC_REFERENCE_TICKER_CIK = {
    "AAPL": "0000320193",
    "MSFT": "0000789019",
}
SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
DEFAULT_SEC_USER_AGENT_PREFIX = "nisa-quant-assistant/phase3-read-only"
MARKET_REQUEST_SCHEMA = "phase3-market-request"
MARKET_REQUEST_SCHEMA_VERSION = 1
MARKET_REQUEST_ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart"
MARKET_REQUEST_USER_AGENT = "nisa-quant-assistant/phase3-read-only"
MIN_YAHOO_EPOCH_SECONDS = -62135596800
MAX_YAHOO_EPOCH_SECONDS = 253402300799
MARKET_REQUEST_FIELDS = frozenset({
    "schema", "schema_version", "asset_tickers", "benchmark_ticker",
    "start_date", "end_date", "period1", "period2", "interval", "events",
    "return_basis", "timeout", "retries", "user_agent", "endpoint",
})
LEGACY_REQUEST_FIELDS = frozenset({"asset_tickers", "benchmark_ticker", "return_basis"})
SUPPORTED_SEC_CONCEPTS = frozenset({
    "us-gaap:Revenues", "us-gaap:SalesRevenueNet",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:NetIncomeLoss", "us-gaap:ProfitLoss",
})
SUPPORTED_SEC_UNITS = frozenset({"USD"})
SUPPORTED_SEC_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
SEC_CACHE_MAX_AGE_DAYS = 31
SEC_REQUEST_SCHEMA = "phase3-sec-request"
SEC_REQUEST_SCHEMA_VERSION = 1
SEC_REQUEST_FIELDS = frozenset({
    "schema", "schema_version", "ticker", "cik", "provider", "source_version",
    "requested_start", "requested_end", "as_of", "retrieved_at", "retrieval_intent",
    "retrieval_status", "facts_hash",
})
MEMBERSHIP_STATUSES = frozenset({"active", "inactive"})
LOOKAHEAD_BIAS_STATUSES = frozenset({"point_in_time", "current_snapshot_only"})
SURVIVORSHIP_BIAS_STATUSES = frozenset({"none", "survivorship_risk_disclosed", "not_claimed"})
MEMBERSHIP_EVIDENCE_STATUSES = frozenset({
    "point_in_time_membership_evidence", "descriptive_survivor_selected_evidence",
})


def _date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True, slots=True)
class MarketBar:
    ticker: str
    observation_date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    adjusted_close: float | None = None
    dividend: float | None = None
    split_factor: float | None = None
    retrieved_at: str = ""
    source: str = ""
    citation: str = ""


@dataclass(frozen=True, slots=True)
class SecFact:
    ticker: str
    concept: str
    unit: str
    value: float
    period_start: str | None
    period_end: str
    filed_at: str
    form: str
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    accession: str = ""
    frame: str | None = None
    retrieved_at: str = ""
    source: str = SEC_COMPANY_FACTS_PROVIDER
    citation: str = ""


@dataclass(frozen=True, slots=True)
class UniverseMember:
    ticker: str
    effective_from: str | None
    effective_to: str | None
    membership_status: str
    lookahead_bias_status: str
    survivorship_bias_status: str
    source: str
    source_version: str
    source_symbol: str | None = None


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    bars_by_ticker: dict[str, list[MarketBar]]
    benchmark_ticker: str
    coverage: dict[str, dict[str, Any]]
    created_at: str
    sec_facts: list[SecFact]
    snapshot_id: str
    source_snapshot_ids: list[str]
    universe: list[UniverseMember]
    cache_kind: str = "market"
    request_contract: str = ""
    ticker_failures: dict[str, str] = field(default_factory=dict)
    sec_request_contract: str = ""


def _is_positive_finite_price(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        number = float(value)
    except OverflowError:
        return False
    return math.isfinite(number) and number > 0


def is_usable_market_bar(bar: Any) -> bool:
    """Return whether a bar satisfies the complete market-bar invariant.

    Zero volume is valid: it represents a known observation with no reported
    traded shares and is handled as a neutral volume signal downstream.
    """
    try:
        _validate_bar(bar, path="market bar")
    except ValueError:
        return False
    return True


def has_point_in_time_membership_evidence(member: "UniverseMember") -> bool:
    """Return whether a universe row has a complete historical membership contract."""
    if not isinstance(member, UniverseMember):
        return False
    if member.membership_status not in MEMBERSHIP_STATUSES:
        return False
    if member.lookahead_bias_status != "point_in_time":
        return False
    if not member.effective_from or not member.effective_to:
        return False
    try:
        effective_from = date.fromisoformat(member.effective_from)
        effective_to = date.fromisoformat(member.effective_to)
    except ValueError:
        return False
    return (
        effective_from <= effective_to
        and isinstance(member.source, str) and bool(member.source.strip())
        and isinstance(member.source_version, str) and bool(member.source_version.strip())
        and member.survivorship_bias_status == "none"
    )


def _reference_universe_for_tickers(tickers: Sequence[str], *, retrieved_at: str) -> list[UniverseMember]:
    return [UniverseMember(
        ticker=ticker, effective_from=None, effective_to=None,
        membership_status="active", lookahead_bias_status="current_snapshot_only",
        survivorship_bias_status="survivorship_risk_disclosed",
        source="configured current S&P 500 reference universe", source_version=retrieved_at,
    ) for ticker in sorted(tickers)]


def current_reference_universe(*, retrieved_at: str | None = None) -> list[UniverseMember]:
    stamp = retrieved_at or _now()
    return _reference_universe_for_tickers(
        ("AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA"), retrieved_at=stamp,
    )


def _require_exact_fields(value: Mapping[str, Any], expected: set[str], *, path: str) -> None:
    fields = set(value)
    if fields != expected:
        missing = sorted(expected - fields)
        extra = sorted(fields - expected)
        detail = f"missing {missing}" if missing else f"unexpected {extra}"
        raise ValueError(f"history {path} has an invalid field set ({detail})")


def _string(value: Any, *, path: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"history {path} must be a non-empty string")
    return value


def _number(value: Any, *, path: str, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool):
        raise ValueError(f"history {path} must be numeric")
    if not isinstance(value, Real):
        raise ValueError(f"history {path} must be numeric")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"history {path} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"history {path} must be finite")
    return number


def _parse_sec_publication_date(value: Any) -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("history SEC fact.filed_at must be a non-empty date or timestamp")
    if len(value) == 10:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("history SEC fact.filed_at must be an ISO date or timestamp") from exc
    try:
        return parse_retrieved_at(value).date()
    except ValueError as exc:
        raise ValueError("history SEC fact.filed_at must be an ISO date or timestamp") from exc


def _validate_bar(bar: MarketBar, *, path: str) -> MarketBar:
    if not isinstance(bar, MarketBar):
        raise ValueError(f"history {path} must be a MarketBar")
    _string(bar.ticker, path=f"{path}.ticker")
    observation_date = _string(bar.observation_date, path=f"{path}.observation_date")
    try:
        normalized_observation_date = date.fromisoformat(observation_date or "")
    except ValueError as exc:
        raise ValueError(f"history {path}.observation_date must be an ISO date") from exc
    if observation_date != normalized_observation_date.isoformat():
        raise ValueError(f"history {path}.observation_date must use canonical ISO date form")
    for field in ("open", "high", "low", "close", "volume", "adjusted_close", "dividend", "split_factor"):
        _number(getattr(bar, field), path=f"{path}.{field}", nullable=True)
    prices = {field: getattr(bar, field) for field in ("open", "high", "low", "close")}
    if any(not _is_positive_finite_price(value) for value in prices.values()):
        raise ValueError(f"history {path} OHLC values must be finite and positive")
    if float(prices["high"]) < max(float(prices["open"]), float(prices["close"]), float(prices["low"])):
        raise ValueError(f"history {path} OHLC high is below an observed price")
    if float(prices["low"]) > min(float(prices["open"]), float(prices["close"]), float(prices["high"])):
        raise ValueError(f"history {path} OHLC low is above an observed price")
    volume = getattr(bar, "volume")
    if volume is None or not isinstance(volume, Real) or isinstance(volume, bool) or not math.isfinite(float(volume)) or float(volume) < 0:
        raise ValueError(f"history {path} volume must be finite and non-negative")
    for field in ("retrieved_at", "source", "citation"):
        value = getattr(bar, field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"history {path}.{field} must be a non-empty string")
    if bar.dividend is not None and float(bar.dividend) < 0:
        raise ValueError(f"history {path}.dividend must be finite and non-negative")
    if bar.split_factor is not None and float(bar.split_factor) <= 0:
        raise ValueError(f"history {path}.split_factor must be finite and positive")
    try:
        retrieved_at = parse_retrieved_at(bar.retrieved_at)
    except ValueError as exc:
        raise ValueError(f"history {path}.retrieved_at must be an ISO timestamp") from exc
    if retrieved_at.date() < normalized_observation_date:
        raise ValueError(f"history {path} retrieved_at precedes observation_date")
    return bar


def _bar_from_dict(value: Mapping[str, Any]) -> MarketBar:
    _require_exact_fields(value, set(MarketBar.__dataclass_fields__), path="market bar")
    bar = MarketBar(
        ticker=value["ticker"], observation_date=value["observation_date"],
        open=value.get("open"), high=value.get("high"), low=value.get("low"),
        close=value.get("close"), volume=value.get("volume"),
        adjusted_close=value.get("adjusted_close"), dividend=value.get("dividend"),
        split_factor=value.get("split_factor"), retrieved_at=value.get("retrieved_at", ""),
        source=value.get("source", ""), citation=value.get("citation", ""),
    )
    try:
        return _validate_bar(bar, path="market bar")
    except ValueError as exc:
        raise ValueError(f"history snapshot identity contains an invalid market bar: {exc}") from exc


def _validate_sec_fact(fact: SecFact) -> SecFact:
    _string(fact.ticker, path="SEC fact.ticker")
    _string(fact.concept, path="SEC fact.concept")
    _string(fact.unit, path="SEC fact.unit")
    _number(fact.value, path="SEC fact.value")
    _string(fact.period_start, path="SEC fact.period_start", nullable=True)
    _string(fact.period_end, path="SEC fact.period_end")
    filed_at = _string(fact.filed_at, path="SEC fact.filed_at")
    filed_date = _parse_sec_publication_date(filed_at)
    try:
        period_end = date.fromisoformat(fact.period_end)
    except ValueError as exc:
        raise ValueError("history SEC fact.period_end must be an ISO date") from exc
    if fact.period_start is not None and date.fromisoformat(fact.period_start) > period_end:
        raise ValueError("history SEC fact period starts after it ends")
    if period_end > filed_date:
        raise ValueError("history SEC fact period follows publication")
    if fact.retrieved_at:
        try:
            retrieved_date = date.fromisoformat(fact.retrieved_at[:10])
        except ValueError as exc:
            raise ValueError("history SEC fact.retrieved_at must be an ISO date or timestamp") from exc
        if filed_date > retrieved_date or period_end > retrieved_date:
            raise ValueError("history SEC fact chronology follows retrieval")
    _string(fact.form, path="SEC fact.form")
    if fact.fiscal_year is not None and (isinstance(fact.fiscal_year, bool) or not isinstance(fact.fiscal_year, int)):
        raise ValueError("history SEC fact.fiscal_year must be an integer or null")
    if fact.fiscal_year is not None and fact.fiscal_year > filed_date.year:
        raise ValueError("history SEC fact fiscal date follows publication")
    _string(fact.fiscal_period, path="SEC fact.fiscal_period", nullable=True)
    if fact.source != SEC_COMPANY_FACTS_PROVIDER:
        raise ValueError("history SEC fact.source is not the canonical SEC Company Facts provider")
    citation = _string(fact.citation, path="SEC fact.citation")
    if not citation.startswith(("https://data.sec.gov/", "https://www.sec.gov/", "fixture://", "fixture-sec:", "sec://")):
        raise ValueError("history SEC fact.citation is not a valid SEC evidence identity")
    accession = _string(fact.accession, path="SEC fact.accession")
    is_official_accession = bool(re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession or ""))
    is_explicit_fixture_accession = bool(
        re.fullmatch(r"acc(?:-[A-Za-z0-9]+)+", accession or "")
        and (citation or "").startswith(("fixture", "sec://"))
    )
    if not (is_official_accession or is_explicit_fixture_accession):
        raise ValueError("history SEC fact.accession is malformed")
    retrieved_at = _string(fact.retrieved_at, path="SEC fact.retrieved_at")
    try:
        retrieved_timestamp = parse_retrieved_at(retrieved_at or "")
    except ValueError as exc:
        raise ValueError("history SEC fact.retrieved_at must be an ISO timestamp") from exc
    if filed_date > retrieved_timestamp.date() or period_end > retrieved_timestamp.date():
        raise ValueError("history SEC fact chronology follows retrieval")
    _string(fact.frame, path="SEC fact.frame", nullable=True)
    return fact


def _sec_from_dict(value: Mapping[str, Any]) -> SecFact:
    _require_exact_fields(value, set(SecFact.__dataclass_fields__), path="SEC fact")
    return _validate_sec_fact(SecFact(**{field: value[field] for field in SecFact.__dataclass_fields__}))  # type: ignore[arg-type]


def _sec_facts_hash(facts: Sequence[SecFact]) -> str:
    body = json.dumps(
        [asdict(fact) for fact in facts], sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()
    return hashlib.sha256(body).hexdigest()


def build_sec_request_contract(
    *, ticker: str | None, cik: str | None, provider: str, source_version: str,
    requested_start: date | str | None, requested_end: date | str | None,
    as_of: date | str | None, retrieved_at: str, retrieval_intent: str,
    retrieval_status: str, facts: Sequence[SecFact],
) -> str:
    normalized_cik = _normalize_sec_cik(cik, path="SEC request CIK") if cik is not None else None
    values = {
        "schema": SEC_REQUEST_SCHEMA, "schema_version": SEC_REQUEST_SCHEMA_VERSION,
        "ticker": ticker.strip().upper() if isinstance(ticker, str) and ticker.strip() else None,
        "cik": normalized_cik, "provider": provider, "source_version": source_version,
        "requested_start": _date(requested_start).isoformat() if requested_start is not None else None,
        "requested_end": _date(requested_end).isoformat() if requested_end is not None else None,
        "as_of": _date(as_of).isoformat() if as_of is not None else None,
        "retrieved_at": retrieved_at, "retrieval_intent": retrieval_intent,
        "retrieval_status": retrieval_status, "facts_hash": _sec_facts_hash(facts),
    }
    contract = json.dumps(values, sort_keys=True, separators=(",", ":"))
    _validate_sec_request_contract(contract, facts=facts)
    return contract


def _default_sec_request_contract(facts: Sequence[SecFact], *, created_at: str) -> str:
    tickers = {fact.ticker.strip().upper() for fact in facts if isinstance(fact.ticker, str) and fact.ticker.strip()}
    accessions = [fact.accession for fact in facts if re.fullmatch(r"\d{10}-\d{2}-\d{6}", fact.accession)]
    ciks = {accession[:10] for accession in accessions}
    return build_sec_request_contract(
        ticker=next(iter(tickers)) if len(tickers) == 1 else None,
        cik=next(iter(ciks)) if len(ciks) == 1 else None,
        provider=SEC_COMPANY_FACTS_PROVIDER, source_version="legacy-fixture-v1",
        requested_start=None, requested_end=None, as_of=None, retrieved_at=created_at,
        retrieval_intent="legacy_fixture" if facts else "sec_not_requested",
        retrieval_status="bound" if facts else "not_requested", facts=facts,
    )


def _validate_sec_request_contract(contract: str, *, facts: Sequence[SecFact]) -> dict[str, Any]:
    try:
        parsed = json.loads(contract)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("history SEC request contract is invalid") from exc
    if not isinstance(parsed, dict) or set(parsed) != SEC_REQUEST_FIELDS:
        raise ValueError("history SEC request contract has an invalid field set")
    if json.dumps(parsed, sort_keys=True, separators=(",", ":")) != contract:
        raise ValueError("history SEC request contract is not canonical")
    if parsed["schema"] != SEC_REQUEST_SCHEMA or parsed["schema_version"] != SEC_REQUEST_SCHEMA_VERSION:
        raise ValueError("history SEC request contract schema version is unsupported")
    for name in ("provider", "source_version", "retrieved_at", "retrieval_intent", "retrieval_status"):
        if not isinstance(parsed[name], str) or not parsed[name].strip():
            raise ValueError(f"history SEC request contract {name} is invalid")
    if parsed["ticker"] is not None and (
        not isinstance(parsed["ticker"], str) or not parsed["ticker"] or parsed["ticker"] != parsed["ticker"].upper()
    ):
        raise ValueError("history SEC request contract ticker is invalid")
    if parsed["cik"] is not None:
        parsed_cik = _normalize_sec_cik(parsed["cik"], path="SEC request CIK")
        if parsed_cik != parsed["cik"]:
            raise ValueError("history SEC request contract CIK is not canonical")
    for name in ("requested_start", "requested_end", "as_of"):
        if parsed[name] is not None:
            try:
                date.fromisoformat(parsed[name])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"history SEC request contract {name} is invalid") from exc
    try:
        parse_retrieved_at(parsed["retrieved_at"])
    except ValueError as exc:
        raise ValueError("history SEC request contract retrieved_at is invalid") from exc
    if parsed["requested_start"] and parsed["requested_end"] and parsed["requested_start"] > parsed["requested_end"]:
        raise ValueError("history SEC request contract date range is invalid")
    if parsed["provider"] != SEC_COMPANY_FACTS_PROVIDER:
        raise ValueError("history SEC request contract provider is not canonical")
    if parsed["source_version"] not in SEC_COMPANY_FACTS_SOURCE_VERSIONS:
        raise ValueError("history SEC request contract source_version is not canonical")
    if parsed["retrieval_status"] not in {
        "not_requested", "unavailable", "bound", "bound_no_usable_facts", "failed", "legacy_fixture",
    }:
        raise ValueError("history SEC request contract retrieval status is invalid")
    retrieval_intent = parsed["retrieval_intent"]
    if retrieval_intent not in {"sec_not_requested", "explicit_company_facts_probe", "legacy_fixture"}:
        raise ValueError("history SEC request contract retrieval intent is invalid")
    if retrieval_intent == "sec_not_requested" and (
        parsed["retrieval_status"] != "not_requested" or parsed["ticker"] is not None or parsed["cik"] is not None
    ):
        raise ValueError("history SEC request contract retrieval intent does not match status")
    if retrieval_intent == "explicit_company_facts_probe" and parsed["retrieval_status"] == "not_requested":
        raise ValueError("history SEC request contract retrieval intent does not match not_requested status")
    if retrieval_intent == "legacy_fixture" and (
        parsed["retrieval_status"] != "bound" or not facts
    ):
        raise ValueError("history SEC legacy fixture contract must be bound with facts")
    if parsed["retrieval_status"] == "bound" and not facts:
        raise ValueError("history SEC bound request contract requires facts")
    if not isinstance(parsed["facts_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", parsed["facts_hash"]):
        raise ValueError("history SEC request contract facts hash is invalid")
    if parsed["facts_hash"] != _sec_facts_hash(facts):
        raise ValueError("history SEC request contract facts payload does not match")
    if parsed["retrieval_status"] in {
        "not_requested", "unavailable", "bound_no_usable_facts", "failed",
    } and facts:
        raise ValueError("history SEC request contract status does not permit SEC facts")
    for fact in facts:
        if parsed["ticker"] is not None and fact.ticker != parsed["ticker"]:
            raise ValueError("history SEC request contract ticker does not match SEC fact")
        if parsed["cik"] is not None and re.fullmatch(r"\d{10}-\d{2}-\d{6}", fact.accession or "") and fact.accession[:10] != parsed["cik"]:
            raise ValueError("history SEC request contract CIK does not match SEC fact")
    return parsed


def _universe_from_dict(value: Mapping[str, Any]) -> UniverseMember:
    _require_exact_fields(value, set(UniverseMember.__dataclass_fields__), path="universe member")
    member = UniverseMember(**{field: value[field] for field in UniverseMember.__dataclass_fields__})
    _string(member.ticker, path="universe member.ticker")
    for field in ("effective_from", "effective_to"):
        value = getattr(member, field)
        _string(value, path=f"universe member.{field}", nullable=True)
        if value is not None:
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"history universe member.{field} must be an ISO date") from exc
    for field, allowed in (
        ("membership_status", MEMBERSHIP_STATUSES),
        ("lookahead_bias_status", LOOKAHEAD_BIAS_STATUSES),
        ("survivorship_bias_status", SURVIVORSHIP_BIAS_STATUSES),
    ):
        status = _string(getattr(member, field), path=f"universe member.{field}")
        if status not in allowed:
            raise ValueError(f"history universe member.{field} has an unsupported status: {status!r}")
    for field in ("source", "source_version"):
        _string(getattr(member, field), path=f"universe member.{field}")
    _string(member.source_symbol, path="universe member.source_symbol", nullable=True)
    if member.effective_from and member.effective_to and date.fromisoformat(member.effective_from) > date.fromisoformat(member.effective_to):
        raise ValueError("history universe member effective_from follows effective_to")
    return member


def _normalize_universe_member(value: UniverseMember | Mapping[str, Any]) -> UniverseMember:
    if isinstance(value, UniverseMember):
        return _universe_from_dict(asdict(value))
    if isinstance(value, Mapping):
        return _universe_from_dict(value)
    raise ValueError("history universe member must be a typed UniverseMember or object")


def validate_universe_member(value: UniverseMember | Mapping[str, Any]) -> UniverseMember:
    """Validate a universe row at every consumer boundary."""
    return _normalize_universe_member(value)


def _request_epoch(value: date, *, end: bool = False) -> int:
    moment = datetime.combine(value + timedelta(days=1 if end else 0), datetime.min.time(), timezone.utc)
    return int(moment.timestamp())


def _validate_request_contract(contract: str, *, benchmark_ticker: str) -> dict[str, Any]:
    try:
        parsed = json.loads(contract)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("history request contract is invalid") from exc
    if not isinstance(parsed, dict):
        raise ValueError("history request contract must be an object")
    if json.dumps(parsed, sort_keys=True, separators=(",", ":")) != contract:
        raise ValueError("history request contract is not canonical")
    if set(parsed) != MARKET_REQUEST_FIELDS:
        missing = sorted(MARKET_REQUEST_FIELDS - set(parsed))
        extra = sorted(set(parsed) - MARKET_REQUEST_FIELDS)
        detail = f"missing {missing}" if missing else f"unexpected {extra}"
        raise ValueError(f"history request contract has invalid schema fields ({detail})")
    if parsed.get("schema") != MARKET_REQUEST_SCHEMA or parsed.get("schema_version") != MARKET_REQUEST_SCHEMA_VERSION:
        raise ValueError("history request contract schema version is unsupported")
    asset_tickers = parsed["asset_tickers"]
    if not isinstance(asset_tickers, list) or not asset_tickers or any(
        not isinstance(ticker, str) or not ticker or ticker != ticker.strip() or ticker != ticker.upper()
        for ticker in asset_tickers
    ):
        raise ValueError("history request contract asset role is invalid")
    if len(set(asset_tickers)) != len(asset_tickers) or asset_tickers != sorted(asset_tickers):
        raise ValueError("history request contract asset role is not canonical")
    if benchmark_ticker in asset_tickers:
        raise ValueError("history request contract asset and benchmark roles must be disjoint")
    if parsed["benchmark_ticker"] != benchmark_ticker:
        raise ValueError("history request contract benchmark role does not match snapshot")
    try:
        start = date.fromisoformat(parsed["start_date"])
        end = date.fromisoformat(parsed["end_date"])
    except (TypeError, ValueError) as exc:
        raise ValueError("history request contract date range is invalid") from exc
    if start > end:
        raise ValueError("history request contract date range is invalid")
    if parsed["period1"] != _request_epoch(start) or parsed["period2"] != _request_epoch(end, end=True):
        raise ValueError("history request contract date range does not match provider periods")
    if parsed["interval"] != "1d" or parsed["events"] != "div,splits":
        raise ValueError("history request contract interval or events are unsupported")
    if parsed["return_basis"] != "price_return":
        raise ValueError("history request contract return basis is not price_return")
    if parsed["user_agent"] != MARKET_REQUEST_USER_AGENT or parsed["endpoint"] != MARKET_REQUEST_ENDPOINT:
        raise ValueError("history request contract provider binding is unsupported")
    timeout = parsed["timeout"]
    if isinstance(timeout, bool) or not isinstance(timeout, Real) or not math.isfinite(float(timeout)) or float(timeout) <= 0:
        raise ValueError("history request contract timeout is invalid")
    retries = parsed["retries"]
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 1:
        raise ValueError("history request contract retries are invalid")
    return parsed


def _normalize_legacy_request_contract(
    contract: str, *, bars_by_ticker: Mapping[str, Sequence[MarketBar]], benchmark_ticker: str,
) -> str:
    try:
        parsed = json.loads(contract)
    except (TypeError, json.JSONDecodeError):
        return contract
    if not isinstance(parsed, dict) or set(parsed) != LEGACY_REQUEST_FIELDS:
        return contract
    asset_tickers = parsed.get("asset_tickers")
    if (
        parsed.get("benchmark_ticker") != benchmark_ticker
        or parsed.get("return_basis") != "price_return"
        or not isinstance(asset_tickers, list)
        or any(ticker not in bars_by_ticker for ticker in asset_tickers)
    ):
        return contract
    observations = [
        bar.observation_date
        for ticker, bars in bars_by_ticker.items()
        if ticker in {*asset_tickers, benchmark_ticker}
        for bar in bars
    ]
    if not observations:
        return contract
    start = min(date.fromisoformat(value) for value in observations)
    end = max(date.fromisoformat(value) for value in observations)
    return json.dumps({
        "schema": MARKET_REQUEST_SCHEMA, "schema_version": MARKET_REQUEST_SCHEMA_VERSION,
        "asset_tickers": sorted(asset_tickers), "benchmark_ticker": benchmark_ticker,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "period1": _request_epoch(start), "period2": _request_epoch(end, end=True),
        "interval": "1d", "events": "div,splits", "return_basis": "price_return",
        "timeout": 15, "retries": 2, "user_agent": MARKET_REQUEST_USER_AGENT,
        "endpoint": MARKET_REQUEST_ENDPOINT,
    }, sort_keys=True, separators=(",", ":"))


def _validate_snapshot_roles(
    bars_by_ticker: Mapping[str, Sequence[MarketBar]], *, request_contract: str,
    benchmark_ticker: str, universe: Sequence[UniverseMember],
) -> None:
    contract = _validate_request_contract(request_contract, benchmark_ticker=benchmark_ticker)
    asset_tickers = set(contract["asset_tickers"])
    for member in universe:
        _universe_from_dict(asdict(member) if isinstance(member, UniverseMember) else member)
    universe_tickers = [member.ticker for member in universe]
    if len(universe_tickers) != len(set(universe_tickers)) or set(universe_tickers) != asset_tickers:
        raise ValueError("history snapshot universe membership roles do not match request contract")
    expected_tickers = asset_tickers | {benchmark_ticker}
    if set(bars_by_ticker) != expected_tickers:
        raise ValueError("history market snapshot ticker roles do not match request contract")
    start = date.fromisoformat(contract["start_date"])
    end = date.fromisoformat(contract["end_date"])
    for ticker, bars in bars_by_ticker.items():
        seen_dates: set[str] = set()
        for bar in bars:
            _validate_bar(bar, path=f"bars_by_ticker[{ticker}]")
            if bar.ticker != ticker:
                raise ValueError(f"history market bar ticker does not match its series key: {ticker}")
            if bar.observation_date in seen_dates:
                raise ValueError(f"history {ticker} contains duplicate observation date {bar.observation_date}")
            seen_dates.add(bar.observation_date)
            observation = date.fromisoformat(bar.observation_date)
            if not start <= observation <= end:
                raise ValueError(
                    f"history bar {ticker} observation {bar.observation_date} is outside "
                    f"the request contract bounds {start.isoformat()}..{end.isoformat()}"
                )


def _content_hash(snapshot: HistorySnapshot) -> str:
    payload = {
        "bars_by_ticker": {key: [asdict(bar) for bar in value] for key, value in sorted(snapshot.bars_by_ticker.items())},
        "benchmark_ticker": snapshot.benchmark_ticker, "coverage": snapshot.coverage,
        "created_at": snapshot.created_at, "sec_facts": [asdict(x) for x in snapshot.sec_facts],
        "source_snapshot_ids": snapshot.source_snapshot_ids,
        "universe": [asdict(member) for member in snapshot.universe],
        "cache_kind": snapshot.cache_kind, "request_contract": snapshot.request_contract,
        "sec_request_contract": snapshot.sec_request_contract,
    }
    if snapshot.ticker_failures:
        payload["ticker_failures"] = snapshot.ticker_failures
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def build_history_snapshot(
    bars_by_ticker: Mapping[str, Sequence[MarketBar]], *, benchmark_ticker: str = "^GSPC",
    universe: Sequence[UniverseMember | Mapping[str, Any]] | None = None, sec_facts: Sequence[SecFact] = (),
    created_at: str | None = None, source_snapshot_ids: Sequence[str] = (),
    cache_kind: str = "market", request_contract: str = "",
    ticker_failures: Mapping[str, str] | None = None,
    invalid_observations_by_ticker: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    sec_request_contract: str | None = None,
) -> HistorySnapshot:
    created = created_at or _now()
    if not isinstance(benchmark_ticker, str) or not benchmark_ticker:
        raise ValueError("history benchmark ticker must be a non-empty string")
    normalized: dict[str, list[MarketBar]] = {}
    invalid_by_ticker: dict[str, list[dict[str, Any]]] = {}
    invalid_failures: dict[str, str] = {}
    raw_counts: dict[str, int] = {}
    for ticker, bars in bars_by_ticker.items():
        if not isinstance(ticker, str) or not ticker:
            raise ValueError("history bar ticker keys must be non-empty strings")
        normalized[ticker] = []
        raw_counts[ticker] = len(bars)
        seen_dates: set[str] = set()
        for bar in bars:
            if not isinstance(bar, MarketBar):
                raise ValueError(f"history bars_by_ticker[{ticker}] must contain MarketBar objects")
            if bar.ticker != ticker:
                raise ValueError("history market bar ticker does not match its series key")
            invalid_fields = [
                field for field in ("open", "high", "low", "close")
                if not _is_positive_finite_price(getattr(bar, field, None))
            ]
            volume = getattr(bar, "volume", None)
            try:
                invalid_volume = (
                    volume is None or isinstance(volume, bool) or not isinstance(volume, Real)
                    or not math.isfinite(float(volume)) or float(volume) < 0
                )
            except OverflowError:
                invalid_volume = True
            if invalid_volume:
                invalid_fields.append("volume")
            try:
                invalid_dividend = (
                    bar.dividend is not None
                    and (
                        isinstance(bar.dividend, bool)
                        or not isinstance(bar.dividend, Real)
                        or not math.isfinite(float(bar.dividend))
                        or float(bar.dividend) < 0
                    )
                )
            except OverflowError:
                invalid_dividend = True
            if invalid_dividend:
                invalid_fields.append("dividend")
            try:
                invalid_split_factor = (
                    bar.split_factor is not None
                    and (
                        isinstance(bar.split_factor, bool)
                        or not isinstance(bar.split_factor, Real)
                        or not math.isfinite(float(bar.split_factor))
                        or float(bar.split_factor) <= 0
                    )
                )
            except OverflowError:
                invalid_split_factor = True
            if invalid_split_factor:
                invalid_fields.append("split_factor")
            try:
                observation_date = date.fromisoformat(bar.observation_date)
            except (TypeError, ValueError):
                _validate_bar(bar, path=f"bars_by_ticker[{ticker}]")
                raise AssertionError("unreachable")
            normalized_observation_date = observation_date.isoformat()
            if bar.observation_date != normalized_observation_date:
                invalid_fields.append("observation_date")
            if not invalid_fields and float(bar.high) < max(float(bar.open), float(bar.close), float(bar.low)):
                invalid_fields.append("high")
            if not invalid_fields and float(bar.low) > min(float(bar.open), float(bar.close), float(bar.high)):
                invalid_fields.append("low")
            if not isinstance(bar.retrieved_at, str) or not bar.retrieved_at.strip():
                invalid_fields.append("retrieved_at")
            else:
                try:
                    retrieved_at = parse_retrieved_at(bar.retrieved_at)
                except ValueError:
                    invalid_fields.append("retrieved_at")
                else:
                    if retrieved_at.date() < observation_date:
                        invalid_fields.append("retrieved_at_chronology")
            for field in ("source", "citation"):
                if not isinstance(getattr(bar, field), str) or not getattr(bar, field).strip():
                    invalid_fields.append(field)
            if normalized_observation_date in seen_dates:
                invalid_fields.append("duplicate_observation_date")
            if invalid_fields:
                invalid_by_ticker.setdefault(ticker, []).append({
                    "observation_date": bar.observation_date,
                    "invalid_fields": sorted(set(invalid_fields)),
                })
                invalid_failures.setdefault(ticker, "invalid_observation: market-bar invariant failed")
                continue
            _validate_bar(bar, path=f"bars_by_ticker[{ticker}]")
            seen_dates.add(normalized_observation_date)
            normalized[ticker].append(bar)
    for series in normalized.values():
        series.sort(key=lambda bar: bar.observation_date)
    if invalid_observations_by_ticker is not None:
        if not isinstance(invalid_observations_by_ticker, Mapping):
            raise ValueError("history invalid observations must be an object")
        for ticker, observations in invalid_observations_by_ticker.items():
            if ticker not in normalized or not isinstance(observations, Sequence):
                raise ValueError("history invalid observations must name known tickers with arrays")
            for observation in observations:
                if not isinstance(observation, Mapping):
                    raise ValueError("history invalid observation evidence must be an object")
                invalid_by_ticker.setdefault(ticker, []).append(dict(observation))
            if observations:
                invalid_failures.setdefault(ticker, "invalid_observation: provider market-bar validation failed")
    coverage: dict[str, dict[str, Any]] = {}
    for ticker, bars in normalized.items():
        dates = [bar.observation_date for bar in bars]
        invalid_observations = invalid_by_ticker.get(ticker, [])
        gaps = []
        for previous, current in zip(dates, dates[1:]):
            if (_date(current) - _date(previous)).days > 7:
                gaps.append({"from": previous, "to": current})
        coverage[ticker] = {
            "actual_first": dates[0] if dates else None, "actual_last": dates[-1] if dates else None,
            "row_count": sum(is_usable_market_bar(bar) for bar in bars),
            "raw_row_count": raw_counts[ticker], "invalid_observation_count": len(invalid_observations),
            "invalid_observations": invalid_observations, "gap_count": len(gaps), "gaps": gaps,
            "adjusted_close_available": any(b.adjusted_close is not None for b in bars),
            "dividend_available": any(b.dividend is not None for b in bars),
            "split_available": any(b.split_factor is not None for b in bars),
            "retrieved_at": created, "source": bars[0].source if bars else "unavailable",
            "coverage_limitation": "provider-returned range; no 30-year availability assumed",
        }
    normalized_contract: dict[str, Any] | None = None
    if request_contract:
        request_contract = _normalize_legacy_request_contract(
            request_contract, bars_by_ticker=normalized, benchmark_ticker=benchmark_ticker,
        )
        normalized_contract = _validate_request_contract(request_contract, benchmark_ticker=benchmark_ticker)
    default_universe = (
        _reference_universe_for_tickers(normalized_contract["asset_tickers"], retrieved_at=created)
        if normalized_contract is not None
        else current_reference_universe(retrieved_at=created)
    )
    universe_rows = [_normalize_universe_member(row) for row in (universe if universe is not None else default_universe)]
    for fact in sec_facts:
        if not isinstance(fact, SecFact):
            raise ValueError("history SEC facts must be typed SecFact objects")
        _validate_sec_fact(fact)
    if sec_request_contract:
        _validate_sec_request_contract(sec_request_contract, facts=sec_facts)
    else:
        sec_request_contract = _default_sec_request_contract(sec_facts, created_at=created)
    if ticker_failures is not None and not isinstance(ticker_failures, Mapping):
        raise ValueError("history ticker failures must be an object")
    normalized_failures = {**invalid_failures, **dict(ticker_failures or {})}
    if any(
        not isinstance(ticker, str) or ticker not in normalized or
        not isinstance(message, str) or not message
        for ticker, message in normalized_failures.items()
    ):
        raise ValueError("history ticker failures must name known tickers with messages")
    if not isinstance(cache_kind, str) or not cache_kind:
        raise ValueError("history cache kind must be a non-empty string")
    if normalized_contract is not None:
        _validate_snapshot_roles(
            normalized, request_contract=request_contract, benchmark_ticker=benchmark_ticker,
            universe=universe_rows,
        )
    if not isinstance(created, str) or not created:
        raise ValueError("history created_at must be a non-empty string")
    if any(not isinstance(value, str) or not value for value in source_snapshot_ids):
        raise ValueError("history source snapshot IDs must be non-empty strings")
    interim = HistorySnapshot(
        normalized, benchmark_ticker, coverage, created, list(sec_facts), "",
        list(source_snapshot_ids), universe_rows, cache_kind, request_contract,
        normalized_failures, sec_request_contract,
    )
    return replace(interim, snapshot_id=f"phase3-{_content_hash(interim)[:20]}")


def save_history_snapshot(snapshot: HistorySnapshot, path: Path) -> None:
    if not snapshot.request_contract:
        raise ValueError("history request contract is missing")
    if snapshot.ticker_failures:
        raise ValueError("partial history cache is not replayable")
    _validate_snapshot_roles(
        snapshot.bars_by_ticker, request_contract=snapshot.request_contract,
        benchmark_ticker=snapshot.benchmark_ticker, universe=snapshot.universe,
    )
    if snapshot.snapshot_id != f"phase3-{_content_hash(snapshot)[:20]}":
        raise ValueError("history snapshot content/hash does not match snapshot identity")
    _validate_sec_request_contract(snapshot.sec_request_contract, facts=snapshot.sec_facts)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(snapshot), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_history_snapshot(
    path: Path, *, expected_cache_kind: str = "market", expected_request_contract: str | None = None,
    expected_sec_request_contract: str | None = None,
) -> HistorySnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("history snapshot must be a JSON object")
    if not isinstance(payload.get("snapshot_id"), str) or not payload["snapshot_id"]:
        raise ValueError("history snapshot identity is missing")
    expected_fields = set(HistorySnapshot.__dataclass_fields__)
    if set(payload) == expected_fields - {"ticker_failures"}:
        payload["ticker_failures"] = {}
    if set(payload) == expected_fields - {"sec_request_contract"}:
        payload["sec_request_contract"] = ""
    if set(payload) != expected_fields:
        raise ValueError("history snapshot has an invalid field set")
    if not isinstance(payload["ticker_failures"], dict):
        raise ValueError("history ticker failures must be an object")
    if payload["ticker_failures"]:
        raise ValueError("partial history cache is not replayable")
    if not isinstance(payload.get("request_contract"), str) or not payload["request_contract"]:
        raise ValueError("history request contract is missing")
    cache_kind = payload["cache_kind"]
    contract = payload["request_contract"]
    benchmark_ticker = payload["benchmark_ticker"]
    if cache_kind != expected_cache_kind:
        raise ValueError("history cache kind does not match requested replay")
    if expected_request_contract is not None and contract != expected_request_contract:
        raise ValueError("history request contract does not match requested replay")
    if not isinstance(benchmark_ticker, str) or not benchmark_ticker:
        raise ValueError("history benchmark ticker is missing")
    _validate_request_contract(contract, benchmark_ticker=benchmark_ticker)
    if path.name.startswith("history-"):
        expected_filename = f"history-{hashlib.sha256(contract.encode()).hexdigest()[:24]}.json"
        if path.name != expected_filename:
            raise ValueError("history cache filename does not match request contract")
    bars_payload = payload["bars_by_ticker"]
    if not isinstance(bars_payload, dict):
        raise ValueError("history bars_by_ticker must be an object")
    bars: dict[str, list[MarketBar]] = {}
    for ticker, items in bars_payload.items():
        if not isinstance(ticker, str) or not isinstance(items, list):
            raise ValueError("history bars_by_ticker has an invalid ticker series")
        if any(not isinstance(item, dict) for item in items):
            raise ValueError("history market bars must be objects")
        bars[ticker] = [_bar_from_dict(item) for item in items]
    facts_payload = payload["sec_facts"]
    if not isinstance(facts_payload, list) or any(not isinstance(item, dict) for item in facts_payload):
        raise ValueError("history sec_facts must be a list of objects")
    facts = [_sec_from_dict(item) for item in facts_payload]
    sec_request_contract = payload["sec_request_contract"]
    if not isinstance(sec_request_contract, str):
        raise ValueError("history SEC request contract must be a string")
    if not sec_request_contract:
        sec_request_contract = _default_sec_request_contract(facts, created_at=payload["created_at"])
    _validate_sec_request_contract(sec_request_contract, facts=facts)
    if expected_sec_request_contract is not None:
        expected_sec = _validate_sec_request_contract(expected_sec_request_contract, facts=())
        actual_sec = _validate_sec_request_contract(sec_request_contract, facts=facts)
        comparable_fields = SEC_REQUEST_FIELDS - {"retrieved_at"}
        if any(actual_sec[field] != expected_sec[field] for field in comparable_fields):
            raise ValueError("history SEC request contract does not match requested replay")
        actual_retrieved = parse_retrieved_at(actual_sec["retrieved_at"])
        expected_retrieved = parse_retrieved_at(expected_sec["retrieved_at"])
        age_days = (expected_retrieved.date() - actual_retrieved.date()).days
        if age_days < 0 or age_days > SEC_CACHE_MAX_AGE_DAYS:
            raise ValueError("history SEC request contract is outside the freshness policy")
    universe_payload = payload["universe"]
    if not isinstance(universe_payload, list) or any(not isinstance(item, dict) for item in universe_payload):
        raise ValueError("history universe must be a list of objects")
    universe = [_universe_from_dict(item) for item in universe_payload]
    _validate_snapshot_roles(
        bars, request_contract=contract, benchmark_ticker=benchmark_ticker, universe=universe,
    )
    source_snapshot_ids = payload["source_snapshot_ids"]
    if not isinstance(source_snapshot_ids, list) or any(not isinstance(item, str) or not item for item in source_snapshot_ids):
        raise ValueError("history source snapshot IDs must be a list of non-empty strings")
    snapshot = build_history_snapshot(
        bars, benchmark_ticker=benchmark_ticker, universe=universe,
        sec_facts=facts, created_at=payload["created_at"], source_snapshot_ids=source_snapshot_ids,
        cache_kind=cache_kind, request_contract=contract,
        ticker_failures=payload["ticker_failures"], sec_request_contract=sec_request_contract,
    )
    if payload["coverage"] != snapshot.coverage:
        raise ValueError("history snapshot content/hash does not match snapshot identity")
    if payload["snapshot_id"] != snapshot.snapshot_id:
        raise ValueError("history snapshot content/hash does not match snapshot identity")
    return snapshot


class RetryableProviderUnavailable(ProviderUnavailable):
    """A provider failure that is safe to retry under the request contract."""


class NonRetryableProviderUnavailable(ProviderUnavailable):
    """A provider response that must not be retried."""


def _http_json(url: str, *, user_agent: str, timeout: float = 15, max_bytes: int = 5_000_000, http_get: Callable[..., HttpResponse] | None = None) -> dict[str, Any]:
    if http_get is not None:
        response = http_get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    else:
        hosts = SEC_HOSTS if "sec.gov" in (urllib.parse.urlparse(url).hostname or "") else YAHOO_HOSTS
        response = UrllibReadOnlyTransport(hosts, max_bytes=max_bytes).get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    if response.status_code != 200:
        if response.status_code in {408, 429} or 500 <= response.status_code <= 599:
            raise RetryableProviderUnavailable(f"provider returned transient HTTP {response.status_code}")
        raise NonRetryableProviderUnavailable(f"provider returned non-retryable HTTP {response.status_code}")
    if len(response.body) > max_bytes:
        raise ProviderUnavailable("provider response exceeds the configured byte limit")
    result = json.loads(response.body.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("provider JSON must be an object")
    return result


def _parse_yahoo(ticker: str, payload: Mapping[str, Any], retrieved_at: str, url: str, *, start_date: date, end_date: date) -> list[MarketBar]:
    if not isinstance(payload, Mapping):
        raise ValueError("Yahoo response must be an object")
    chart = payload.get("chart")
    if not isinstance(chart, Mapping):
        raise ValueError("Yahoo chart block must be an object")
    result = chart.get("result")
    if result == []:
        raise ProviderUnavailable(f"Yahoo returned no chart data for {ticker}")
    if not isinstance(result, list) or not result:
        raise ValueError("Yahoo chart result must be a non-empty array")
    result = result[0]
    if not isinstance(result, Mapping):
        raise ValueError("Yahoo chart result must be an object")
    meta = result.get("meta")
    supplied_symbol = meta.get("symbol") if isinstance(meta, Mapping) else None
    normalized_ticker = urllib.parse.unquote(ticker).upper()
    if not isinstance(supplied_symbol, str) or not supplied_symbol or supplied_symbol.upper() != normalized_ticker:
        raise ValueError(f"Yahoo response identity does not match requested ticker {ticker}")
    timestamps = result.get("timestamp", [])
    if not isinstance(timestamps, list) or not timestamps:
        raise ValueError("Yahoo timestamp array is invalid")
    if any(
        isinstance(stamp, bool) or not isinstance(stamp, int)
        or not MIN_YAHOO_EPOCH_SECONDS <= stamp <= MAX_YAHOO_EPOCH_SECONDS
        for stamp in timestamps
    ):
        raise ValueError("Yahoo timestamp array must contain in-range integer epoch seconds")
    if len(set(timestamps)) != len(timestamps):
        raise ValueError("Yahoo timestamp array contains duplicate timestamps")
    quote_block = result.get("indicators", {}).get("quote", []) if isinstance(result.get("indicators"), Mapping) else []
    if not isinstance(quote_block, list) or not quote_block or not isinstance(quote_block[0], Mapping):
        raise ValueError("Yahoo quote arrays are missing")
    quote = quote_block[0]
    adjusted_block = result.get("indicators", {}).get("adjclose", []) if isinstance(result.get("indicators"), Mapping) else []
    adjusted: list[Any] = [None] * len(timestamps)
    if adjusted_block:
        if not isinstance(adjusted_block, list) or not isinstance(adjusted_block[0], Mapping):
            raise ValueError("Yahoo adjusted-close array is invalid")
        adjusted = adjusted_block[0].get("adjclose", [])
        if not isinstance(adjusted, list) or len(adjusted) != len(timestamps):
            raise ValueError("Yahoo adjusted-close array length does not match timestamps")
    for name in ("open", "high", "low", "close", "volume"):
        values = quote.get(name)
        if not isinstance(values, list) or len(values) != len(timestamps):
            raise ValueError(f"Yahoo {name} array length does not match timestamps")
        for value in values:
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or (name != "volume" and float(value) <= 0)
                or (name == "volume" and float(value) < 0)
            ):
                raise ValueError(f"Yahoo {name} array has invalid market values")
            if value is None:
                raise ValueError(f"Yahoo {name} array has incomplete market values")
    for index in range(len(timestamps)):
        opening = float(quote["open"][index])
        high = float(quote["high"][index])
        low = float(quote["low"][index])
        close = float(quote["close"][index])
        if high < max(opening, close, low) or low > min(opening, close, high):
            raise ValueError(f"Yahoo OHLC relationship is invalid at timestamp {timestamps[index]}")
    events = result.get("events", {})
    if not isinstance(events, Mapping):
        raise ValueError("Yahoo events block is invalid")
    dividends = events.get("dividends", events.get("div", {}))
    splits = events.get("splits", {})
    if not isinstance(dividends, Mapping) or not isinstance(splits, Mapping):
        raise ValueError("Yahoo event series is invalid")
    for event_name, event_series in (("div", dividends), ("splits", splits)):
        if any(not isinstance(event, Mapping) for event in event_series.values()):
            raise ValueError(f"Yahoo {event_name} event series is invalid")
    dividend_amounts: dict[str, float] = {}
    for event_key, event in dividends.items():
        amount = event.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValueError("Yahoo dividend event has an invalid amount")
        try:
            amount = float(amount)
        except (OverflowError, ValueError) as exc:
            raise ValueError("Yahoo dividend event has an invalid amount") from exc
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("Yahoo dividend event has an invalid amount")
        dividend_amounts[str(event_key)] = amount
    split_factors: dict[str, float] = {}
    for event_key, event in splits.items():
        numerator = event.get("numerator")
        denominator = event.get("denominator")
        if (
            isinstance(numerator, bool) or not isinstance(numerator, (int, float))
            or isinstance(denominator, bool) or not isinstance(denominator, (int, float))
        ):
            raise ValueError("Yahoo split event has an invalid ratio")
        try:
            numerator = float(numerator)
            denominator = float(denominator)
        except (OverflowError, ValueError) as exc:
            raise ValueError("Yahoo split event has an invalid ratio") from exc
        if not math.isfinite(numerator) or not math.isfinite(denominator) or numerator <= 0 or denominator <= 0:
            raise ValueError("Yahoo split event has an invalid ratio")
        try:
            factor = numerator / denominator
        except (OverflowError, ZeroDivisionError) as exc:
            raise ValueError("Yahoo split event has an invalid ratio") from exc
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("Yahoo split event has an invalid ratio")
        split_factors[str(event_key)] = factor
    bars = []
    seen_observation_dates: set[str] = set()
    for index, stamp in enumerate(timestamps):
        try:
            day_value = datetime.fromtimestamp(stamp, timezone.utc).date()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("Yahoo timestamp is outside the supported epoch range") from exc
        day = day_value.isoformat()
        if day_value < start_date or day_value > end_date:
            raise ValueError("Yahoo observation is outside the requested date bounds")
        if day in seen_observation_dates:
            raise ValueError(f"Yahoo observation dates contain duplicate normalized date {day}")
        seen_observation_dates.add(day)
        def get(name: str) -> float | None:
            value = quote[name][index]
            return float(value) if value is not None else None
        div = dividend_amounts.get(str(stamp))
        split_factor = split_factors.get(str(stamp))
        adjusted_value = adjusted[index]
        if adjusted_value is not None and (isinstance(adjusted_value, bool) or not isinstance(adjusted_value, (int, float)) or not math.isfinite(float(adjusted_value)) or float(adjusted_value) <= 0):
            raise ValueError("Yahoo adjusted-close array has invalid types")
        bar = MarketBar(
            ticker, day, get("open"), get("high"), get("low"), get("close"), get("volume"),
            float(adjusted_value) if adjusted_value is not None else None, div, split_factor,
            retrieved_at, f"Yahoo Finance chart (unofficial reference); url={url}",
            f"{url}: ticker={ticker}, date={day}",
        )
        _validate_bar(bar, path=f"Yahoo market bar {ticker}")
        bars.append(bar)
    return bars


def fetch_history_snapshot(
    *, tickers: Sequence[str] | None = None, benchmark_ticker: str = "^GSPC", start_date: date | str | None = None,
    end_date: date | str | None = None, cache_dir: Path = Path("data/phase3"), retrieved_at: str | None = None,
    timeout: float = 15, retries: int = 2, http_get: Callable[..., HttpResponse] | None = None,
    universe: Sequence[UniverseMember | Mapping[str, Any]] | None = None,
    sec_facts: Sequence[SecFact] = (), allow_partial: bool = False,
    sec_request_contract: str | None = None, allow_cache_reuse: bool = True,
) -> HistorySnapshot:
    if tickers is None or not tickers:
        raise ValueError("explicit ticker universe is required; the six-symbol fixture is offline-only")
    tickers = list(tickers)
    start = _date(start_date or (date.today() - timedelta(days=365 * 30)))
    end = _date(end_date or date.today())
    stamp = retrieved_at or _now()
    period1, period2 = int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()), int(datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
    contract = json.dumps({
        "schema": MARKET_REQUEST_SCHEMA, "schema_version": MARKET_REQUEST_SCHEMA_VERSION,
        "asset_tickers": sorted(tickers), "benchmark_ticker": benchmark_ticker,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "period1": period1, "period2": period2, "interval": "1d", "events": "div,splits",
        "return_basis": "price_return", "timeout": timeout, "retries": retries,
        "user_agent": MARKET_REQUEST_USER_AGENT,
        "endpoint": MARKET_REQUEST_ENDPOINT,
    }, sort_keys=True, separators=(",", ":"))
    _validate_request_contract(contract, benchmark_ticker=benchmark_ticker)
    expected_sec_request_contract = sec_request_contract or build_sec_request_contract(
        ticker=None, cik=None, provider="SEC XBRL Company Facts", source_version="companyfacts-v1",
        requested_start=start, requested_end=end, as_of=end, retrieved_at=stamp,
        retrieval_intent="sec_not_requested", retrieval_status="not_requested", facts=(),
    )
    _validate_sec_request_contract(expected_sec_request_contract, facts=sec_facts if sec_facts else ())
    key = hashlib.sha256(contract.encode()).hexdigest()[:24]
    path = cache_dir / f"history-{key}.json"
    if path.exists() and allow_cache_reuse and http_get is None and not sec_facts:
        try:
            return load_history_snapshot(
                path, expected_cache_kind="market", expected_request_contract=contract,
                expected_sec_request_contract=expected_sec_request_contract,
            )
        except ValueError:
            pass
    bars: dict[str, list[MarketBar]] = {}
    ids: list[str] = []
    ticker_failures: dict[str, str] = {}
    invalid_observations_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for ticker in [*sorted(tickers), benchmark_ticker]:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker, safe='')}?period1={period1}&period2={period2}&interval=1d&events=div%2Csplits"
        try:
            payload = None
            for attempt in range(retries):
                try:
                    payload = _http_json(url, user_agent="nisa-quant-assistant/phase3-read-only", timeout=timeout, http_get=http_get)
                    break
                except RetryableProviderUnavailable:
                    if attempt + 1 >= retries:
                        raise
                    time.sleep(0.05)
            bars[ticker] = _parse_yahoo(ticker, payload or {}, stamp, url, start_date=start, end_date=end)
            ids.append(f"yahoo:{ticker}:{start.isoformat()}:{end.isoformat()}")
        except (OSError, ValueError, ProviderUnavailable, OverflowError, ZeroDivisionError) as exc:
            if not allow_partial:
                raise
            bars[ticker] = []
            ticker_failures[ticker] = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, (ValueError, OverflowError, ZeroDivisionError)):
                invalid_fields = ["invalid_observation"]
                message = str(exc).casefold()
                if "dividend" in message:
                    invalid_fields.append("dividend")
                if "split" in message:
                    invalid_fields.append("split_factor")
                invalid_observations_by_ticker[ticker] = [{
                    "observation_date": None,
                    "invalid_fields": invalid_fields,
                    "reason": str(exc),
                }]
    snapshot = build_history_snapshot(
        bars, benchmark_ticker=benchmark_ticker, universe=universe, sec_facts=sec_facts,
        created_at=stamp, source_snapshot_ids=ids, request_contract=contract,
        ticker_failures=ticker_failures, sec_request_contract=sec_request_contract or expected_sec_request_contract,
        invalid_observations_by_ticker=invalid_observations_by_ticker,
    )
    if not snapshot.ticker_failures:
        save_history_snapshot(snapshot, path)
    return snapshot


def _normalize_sec_cik(value: Any, *, path: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{path} is missing or malformed")
    digits = str(value).strip()
    if not digits.isdigit() or len(digits) > 10 or set(digits) == {"0"}:
        raise ValueError(f"{path} is missing or malformed")
    return digits.zfill(10)


def _validate_sec_company_facts_url(source_url: str) -> str | None:
    parsed = urllib.parse.urlsplit(source_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "data.sec.gov"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("SEC Company Facts source URL is not the canonical official SEC endpoint")
    base_path = urllib.parse.urlsplit(SEC_COMPANY_FACTS_BASE_URL).path
    if parsed.path == base_path:
        return None
    match = re.fullmatch(rf"{re.escape(base_path)}CIK(\d{{10}})\.json", parsed.path)
    if not match:
        raise ValueError("SEC Company Facts source URL is not the canonical official SEC endpoint")
    return match.group(1)


def parse_sec_company_facts(ticker: str, payload: Mapping[str, Any], *, cik: str | None = None, retrieved_at: str | None = None, source_url: str = SEC_COMPANY_FACTS_BASE_URL, issues: list[str] | None = None) -> list[SecFact]:
    if not isinstance(payload, Mapping):
        raise ValueError("SEC facts payload must be an object")
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("SEC Company Facts ticker identity is invalid")
    url_cik = _validate_sec_company_facts_url(source_url)
    retrieved = _now() if retrieved_at is None else retrieved_at
    try:
        parse_retrieved_at(retrieved)
    except ValueError as exc:
        raise ValueError("SEC Company Facts retrieval timestamp is invalid") from exc
    facts = payload.get("facts", {})
    if not isinstance(facts, Mapping):
        raise ValueError("SEC facts block is invalid")
    try:
        payload_cik = _normalize_sec_cik(payload.get("cik"), path="SEC Company Facts response CIK")
        requested_cik = _normalize_sec_cik(cik, path="SEC requested CIK") if cik is not None else payload_cik
    except ValueError as exc:
        if issues is not None:
            issues.append(str(exc))
            if payload.get("cik") is None:
                for concepts in facts.values():
                    if not isinstance(concepts, Mapping):
                        continue
                    for definition in concepts.values():
                        units = definition.get("units") if isinstance(definition, Mapping) else None
                        if not isinstance(units, Mapping):
                            continue
                        for rows in units.values():
                            if not isinstance(rows, list):
                                continue
                            if any(isinstance(row, Mapping) and not row.get("filed") for row in rows):
                                issues.append("missing SEC filing date")
            return []
        raise
    if payload_cik != requested_cik:
        raise ValueError("SEC Company Facts response CIK does not match the requested CIK")
    if url_cik is not None and url_cik != payload_cik:
        raise ValueError("SEC Company Facts source URL CIK does not match the response CIK")
    normalized_ticker = ticker.strip().upper().replace(".", "-")
    expected_reference_cik = SEC_REFERENCE_TICKER_CIK.get(normalized_ticker)
    if expected_reference_cik is not None and expected_reference_cik != requested_cik:
        raise ValueError("SEC Company Facts ticker does not match the requested issuer CIK")
    payload_ticker = payload.get("ticker")
    if payload_ticker is not None and (
        not isinstance(payload_ticker, str)
        or payload_ticker.strip().upper().replace(".", "-") != normalized_ticker
    ):
        raise ValueError("SEC Company Facts response ticker does not match the requested ticker")
    entity_name = payload.get("entityName")
    if entity_name is not None and (not isinstance(entity_name, str) or not entity_name.strip()):
        message = "SEC Company Facts response entity identity is invalid"
        if issues is not None:
            issues.append(message)
            return []
        raise ValueError(message)
    try:
        retrieved_date = date.fromisoformat(retrieved[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError("SEC Company Facts retrieval timestamp is invalid") from exc
    output: list[SecFact] = []
    for namespace, concepts in facts.items():
        if not isinstance(concepts, Mapping):
            raise ValueError("SEC facts namespace must be an object")
        for name, definition in concepts.items():
            if not isinstance(definition, Mapping):
                raise ValueError("SEC fact definition must be an object")
            concept = f"{namespace}:{name}"
            if concept not in SUPPORTED_SEC_CONCEPTS:
                if issues is not None: issues.append(f"unsupported SEC concept: {concept}")
                continue
            units = definition.get("units", {})
            if not isinstance(units, Mapping):
                raise ValueError("SEC fact units block is invalid")
            for unit, rows in units.items():
                if unit not in SUPPORTED_SEC_UNITS:
                    if issues is not None: issues.append(f"unsupported SEC unit: {unit}")
                    continue
                if not isinstance(rows, list):
                    raise ValueError("SEC fact unit rows must be an array")
                for row in rows:
                    if not isinstance(row, Mapping):
                        raise ValueError("SEC fact row must be an object")
                    form = row.get("form")
                    if form not in SUPPORTED_SEC_FORMS:
                        if issues is not None: issues.append(f"unsupported SEC form: {form}")
                        continue
                    try:
                        value = float(row["val"])
                        end = date.fromisoformat(row["end"])
                        if end.isoformat() != row["end"]:
                            raise ValueError
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not math.isfinite(value): continue
                    start = row.get("start")
                    if start:
                        try:
                            start_date = date.fromisoformat(start)
                            if start_date.isoformat() != start or start_date > end:
                                raise ValueError
                            start = start_date.isoformat()
                        except ValueError: continue
                    filed = row.get("filed")
                    if not isinstance(filed, str) or not filed.strip():
                        if issues is not None:
                            issues.append(f"missing SEC filing date: {concept}")
                        continue
                    try:
                        filed = date.fromisoformat(filed[:10]).isoformat()
                    except ValueError:
                        if issues is not None:
                            issues.append(f"invalid SEC filing date: {concept}")
                        continue
                    filed_date = date.fromisoformat(filed)
                    fiscal_year = row.get("fy")
                    if fiscal_year is not None and (
                        isinstance(fiscal_year, bool) or not isinstance(fiscal_year, int)
                        or fiscal_year > filed_date.year or fiscal_year > retrieved_date.year
                    ):
                        message = f"SEC Company Facts fiscal date is impossible: {concept}"
                        if issues is not None:
                            issues.append(message)
                            continue
                        raise ValueError(message)
                    if end > filed_date or end > retrieved_date:
                        message = f"SEC Company Facts period follows publication or retrieval: {concept}"
                        if issues is not None:
                            issues.append(message)
                            continue
                        raise ValueError(message)
                    accession = row.get("accn")
                    if not isinstance(accession, str) or not accession.strip():
                        message = f"SEC Company Facts accession is missing: {concept}"
                        if issues is not None:
                            issues.append(message)
                            continue
                        raise ValueError(message)
                    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or accession[:10] != payload_cik:
                        raise ValueError(f"SEC Company Facts accession CIK is malformed or mismatched: {accession}")
                    output.append(SecFact(ticker, concept, unit, value, start, end.isoformat(), filed, form, fiscal_year, row.get("fp"), accession, row.get("frame"), retrieved, "SEC XBRL Company Facts", f"{source_url} ticker={ticker} concept={concept} end={end.isoformat()}"))
    return output


def fetch_sec_company_facts(ticker: str, cik: str, *, contact: str | None = None, http_get: Callable[..., HttpResponse] | None = None, retrieved_at: str | None = None, issues: list[str] | None = None) -> list[SecFact]:
    if http_get is None and (not isinstance(contact, str) or "@" not in contact):
        raise ValueError("SEC contact email is required for live retrieval")
    requested_cik = _normalize_sec_cik(cik, path="SEC requested CIK")
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("SEC requested ticker identity is invalid")
    user_agent = f"{DEFAULT_SEC_USER_AGENT_PREFIX} (contact: {contact or '[configured]'} )"
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{requested_cik}.json"
    payload = _http_json(url, user_agent=user_agent, http_get=http_get)
    return parse_sec_company_facts(ticker, payload, cik=requested_cik, retrieved_at=retrieved_at, source_url=url, issues=issues)


def parse_sec_ticker_mapping(payload: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(payload, Mapping):
        raise ValueError("SEC ticker mapping must be an object")
    mapping: dict[str, str] = {}
    for item in payload.values():
        if not isinstance(item, Mapping):
            continue
        ticker = item.get("ticker")
        cik = item.get("cik_str")
        if not isinstance(ticker, str) or not ticker.strip() or isinstance(cik, bool) or not isinstance(cik, (int, str)):
            continue
        digits = str(cik).strip()
        if not digits.isdigit() or len(digits) > 10:
            continue
        normalized = ticker.strip().upper().replace(".", "-")
        if normalized in mapping and mapping[normalized] != digits.zfill(10):
            raise ValueError(f"SEC ticker mapping has conflicting CIKs for {normalized}")
        mapping[normalized] = digits.zfill(10)
    if not mapping:
        raise ValueError("SEC ticker mapping contains no usable entries")
    return mapping


def fetch_sec_company_facts_for_ticker(
    ticker: str, *, contact: str | None = None, http_get: Callable[..., HttpResponse] | None = None,
    retrieved_at: str | None = None, issues: list[str] | None = None,
) -> list[SecFact]:
    if http_get is None and (not isinstance(contact, str) or "@" not in contact):
        raise ValueError("SEC contact email is required for live retrieval")
    user_agent = f"{DEFAULT_SEC_USER_AGENT_PREFIX} (contact: {contact or '[configured]'} )"
    mapping_payload = _http_json(SEC_TICKER_MAP_URL, user_agent=user_agent, http_get=http_get)
    mapping = parse_sec_ticker_mapping(mapping_payload)
    normalized = str(ticker).strip().upper().replace(".", "-")
    if normalized not in mapping:
        raise ValueError(f"SEC ticker is not present in the official ticker mapping: {ticker}")
    return fetch_sec_company_facts(normalized, mapping[normalized], contact=contact, http_get=http_get, retrieved_at=retrieved_at, issues=issues)
