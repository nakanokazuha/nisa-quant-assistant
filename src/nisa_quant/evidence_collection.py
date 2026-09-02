"""Read-only Phase 2 universe, market, and evidence persistence."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .evidence_providers import (
    EvidenceRecord,
    ALPHA_VANTAGE_HOSTS,
    FixtureMarketProvider,
    MarketObservation,
    MarketProvider,
    ProviderUnavailable,
    RequestRateLimiter,
    RssFeedProvider,
    RssSourceConfig,
    SECEdgarProvider,
    UrllibReadOnlyTransport,
    contains_control_content,
    canonical_market_observation,
    normalize_rss_feed,
    normalize_sec_company_facts,
    normalize_sec_submissions,
    SUPPORTED_SEC_FORMS,
    validate_public_reference as _validate_public_reference_impl,
    validate_provider_url,
)
from .source_records import normalize_retrieved_at, parse_retrieved_at, utc_now


UNIVERSE_COLUMNS = (
    "universe_id", "effective_date", "membership_status", "ticker", "cik",
    "issuer_name", "exchange", "source_url", "source_version", "retrieved_at",
    "lookahead_bias_status", "survivorship_bias_status",
)
MARKET_FIELDS = ("open", "high", "low", "close", "volume")
FRESHNESS_STATUSES = frozenset({"current", "observed", "stale", "conflicting", "unavailable"})
RECENCY_STATUSES = frozenset({"recent", "old", "stale", "unknown"})
US_EXCHANGES = frozenset({
    "NYSE", "NASDAQ", "NYSE AMERICAN", "CBOE", "NASDAQ GLOBAL SELECT MARKET",
    "NASDAQ GLOBAL MARKET", "NASDAQ CAPITAL MARKET",
})
TEXT_FACT_FIELDS = frozenset({"form"})
PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
SOURCE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REQUEST_ID_SENSITIVE_SHAPE = re.compile(
    r"(?:^|[-_:])(?:api[-_]?key|access[-_]?token|token|secret|password|credential|authorization|"
    r"broker(?:[-_:](?:login|credential|token|reference?|id|account|client|customer|portfolio))?|"
            r"(?:account|client|customer|portfolio)(?:[-_:](?:id|identifier|number|no))?|"
    r"buy(?:[-_:]candidate)?|hold|sell|watch|order|execute|trade|recommendation|verdict)"
    r"(?:$|[-_:])",
    re.IGNORECASE,
)


class _RequestScopeConflict(ValueError):
    """An existing request cannot be rebound to a different immutable scope."""


class _EvidenceAfterScopeCutoff(ValueError):
    """Evidence cannot be persisted when its publication or retrieval is future-dated."""


@dataclass(frozen=True, slots=True)
class Phase2RefreshResult:
    run_id: str
    accepted_universe_members: int
    accepted_market_observations: int
    accepted_evidence: int
    failure_count: int
    snapshot_id: str
    status: str = "completed"


def _canonical_id(prefix: str, payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"{prefix}-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24]}"


def _iso_date(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be ISO YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be ISO YYYY-MM-DD")
    return value


def _require_text(row: Mapping[str, object], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Phase 2 universe field {name} is required")
    return value.strip()


def _validate_request_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not REQUEST_ID.fullmatch(value)
        or contains_control_content(value)
        or REQUEST_ID_SENSITIVE_SHAPE.search(value)
    ):
        raise ValueError("Phase 2 request_id must be a simple typed identifier")


def _validate_scope_request(
    connection: sqlite3.Connection, *, request_id: str, scope_id: str,
) -> None:
    row = connection.execute(
        "SELECT request_id FROM phase2_refresh_scopes WHERE scope_id = ?", (scope_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Phase 2 refresh scope does not exist")
    if row["request_id"] != request_id:
        raise ValueError("Phase 2 refresh scope is bound to a different request")


def _immutable_scope_cutoff(
    connection: sqlite3.Connection, *, scope_id: str, requested_cutoff: str | None,
    operation: str,
) -> str:
    row = connection.execute(
        "SELECT as_of FROM phase2_refresh_scopes WHERE scope_id = ?", (scope_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"{operation} refresh scope does not exist")
    scope_cutoff = str(row["as_of"])
    if requested_cutoff is not None and requested_cutoff != scope_cutoff:
        raise ValueError(f"{operation} refresh cannot override the immutable scope cutoff")
    return scope_cutoff


def _request_scope_id(connection: sqlite3.Connection, *, request_id: str) -> str | None:
    row = connection.execute(
        "SELECT scope_id FROM phase2_refresh_run_scopes WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    return None if row is None else str(row["scope_id"])


def _validate_public_reference(
    value: str, label: str, *, allow_fixture: bool = False, resolve_host: bool = False,
) -> None:
    _validate_public_reference_impl(value, label, allow_fixture=allow_fixture, resolve_host=resolve_host)


def _event_timestamp(value: str) -> str:
    return normalize_retrieved_at(value)


def _record_failure(
    connection: sqlite3.Connection, *, request_id: str, source_name: str,
    failure_code: str, message: str, ticker: str | None = None,
    observed_at: str | None = None, sensitive_values: Sequence[str] = (),
    scope_id: str | None = None,
) -> None:
    _validate_request_id(request_id)
    raw_message = re.sub(
        r"((?:api[_-]?key|access[_-]?token|token|secret|password|credential)\s*[=:]\s*)[^\s,&]+",
        r"\1[REDACTED]", str(message), flags=re.IGNORECASE,
    )
    raw_message = re.sub(r"\bBearer\s+[^\s]+", "Bearer [REDACTED]", raw_message, flags=re.IGNORECASE)
    for secret in sensitive_values:
        if isinstance(secret, str) and secret:
            raw_message = raw_message.replace(secret, "[REDACTED]")
    if contains_control_content(raw_message):
        safe_message = "Phase 2 input was rejected for unsafe control or credential content"
    else:
        safe_message = raw_message.replace("\n", " ")[:500]
    safe_source_name = (
        source_name
        if isinstance(source_name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9 ._-]{1,63}", source_name)
        else "phase2-validation"
    )
    if contains_control_content(safe_source_name):
        safe_source_name = "phase2-validation"
    safe_ticker = ticker.upper() if isinstance(ticker, str) and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker.upper()) else None
    try:
        normalized_observed = None if observed_at is None else _event_timestamp(observed_at)
    except (TypeError, ValueError):
        normalized_observed = None
    safe_failure_code = (
        failure_code
        if isinstance(failure_code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", failure_code)
        else "input_rejected"
    )
    if contains_control_content(safe_failure_code):
        safe_failure_code = "input_rejected"
    if scope_id is None:
        scope_id = _request_scope_id(connection, request_id=request_id)
    if scope_id is None:
        failure_cutoff = (
            normalized_observed[:10]
            if normalized_observed is not None
            else utc_now()[:10]
        )
        scope_id = _ensure_request_scope(
            connection, request_id=request_id, as_of=failure_cutoff,
            member_ids=[],
        )
    _validate_scope_request(connection, request_id=request_id, scope_id=scope_id)
    failure_id = _canonical_id(
        "P2F",
        [request_id, scope_id, safe_source_name, safe_failure_code, safe_message, safe_ticker, normalized_observed],
    )
    connection.execute(
        """INSERT OR IGNORE INTO phase2_failures(
            failure_id, request_id, scope_id, source_name, failure_code, message, ticker, observed_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (failure_id, request_id, scope_id, safe_source_name, safe_failure_code, safe_message, safe_ticker, normalized_observed,
         datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
    )


def _read_universe_tickers(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != UNIVERSE_COLUMNS:
            raise ValueError("Phase 2 universe must use the documented exact columns")
        return [str(row.get("ticker", "")).strip().upper() for row in reader]


def _current_universe_member_ids(path: Path) -> list[str]:
    """Return only the exact member identities supplied by this refresh input."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != UNIVERSE_COLUMNS:
            raise ValueError("Phase 2 universe must use the documented exact columns")
        member_ids: list[str] = []
        for row in reader:
            values = {name: _require_text(row, name) for name in UNIVERSE_COLUMNS}
            values["ticker"] = values["ticker"].upper()
            member_ids.append(_canonical_id("P2U", values))
        return member_ids


def _latest_member_rows(
    connection: sqlite3.Connection, member_ids: Sequence[str], *, as_of: str,
) -> list[sqlite3.Row]:
    if not member_ids:
        return []
    placeholders = ",".join("?" for _ in member_ids)
    all_states = connection.execute(
        f"""SELECT ticker, cik, membership_status, effective_date, retrieved_at, member_id,
                   issuer_name
            FROM phase2_universe_members
            WHERE member_id IN ({placeholders})
              AND effective_date <= ? AND substr(retrieved_at, 1, 10) <= ?
            ORDER BY ticker, cik, effective_date DESC, retrieved_at DESC, member_id DESC""",
        (*member_ids, as_of, as_of),
    ).fetchall()
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for row in all_states:
        latest.setdefault((row["ticker"], row["cik"]), row)
    return sorted(
        (row for row in latest.values() if row["membership_status"] == "active"),
        key=lambda row: (row["ticker"], row["cik"], row["effective_date"], row["member_id"]),
    )


def _selected_current_members(
    connection: sqlite3.Connection, universe_path: Path, *, as_of: str,
) -> list[sqlite3.Row]:
    return _latest_member_rows(connection, _current_universe_member_ids(universe_path), as_of=as_of)


def _active_member_ids(
    connection: sqlite3.Connection, *, as_of: str,
) -> list[str]:
    member_ids = [
        str(row[0]) for row in connection.execute(
            "SELECT member_id FROM phase2_universe_members ORDER BY member_id",
        ).fetchall()
    ]
    return sorted(
        str(row["member_id"])
        for row in _latest_member_rows(connection, member_ids, as_of=as_of)
    )


def _resolve_exact_target_member_ids(
    connection: sqlite3.Connection,
    targets: Sequence[tuple[str, str | None]],
    *,
    as_of: str,
) -> list[str]:
    """Resolve only the exact active universe members named by a direct request."""
    all_member_ids = [
        str(row[0]) for row in connection.execute(
            "SELECT member_id FROM phase2_universe_members ORDER BY member_id",
        ).fetchall()
    ]
    active_rows = _latest_member_rows(connection, all_member_ids, as_of=as_of)
    resolved: set[str] = set()
    for ticker, cik in sorted(set(targets)):
        matches = [
            row for row in active_rows
            if row["ticker"] == ticker and (cik is None or row["cik"] == cik)
        ]
        if cik is None and len(matches) > 1:
            raise ValueError("direct Phase 2 target has an ambiguous active universe mapping")
        resolved.update(str(row["member_id"]) for row in matches)
    return sorted(resolved)


def _direct_evidence_target_member_ids(
    connection: sqlite3.Connection, records: Sequence[EvidenceRecord], *, as_of: str,
) -> list[str]:
    targets: list[tuple[str, str | None]] = []
    for record in records:
        ticker = record.ticker.upper() if isinstance(record.ticker, str) else ""
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
            continue
        cik = record.issuer_cik
        if isinstance(cik, str) and re.fullmatch(r"\d{1,10}", cik):
            cik = cik.zfill(10)
        else:
            cik = None
        targets.append((ticker, cik))
    return _resolve_exact_target_member_ids(connection, targets, as_of=as_of)


def _ensure_request_scope(
    connection: sqlite3.Connection, *, request_id: str, as_of: str,
    member_ids: Sequence[str] | None = None,
) -> str:
    """Return the immutable request scope used by a direct Phase 2 operation."""
    if member_ids is None:
        raise ValueError("direct Phase 2 scope requires exact target membership")
    normalized_members = member_ids
    return _persist_refresh_scope(
        connection, request_id=request_id, as_of=as_of, member_ids=normalized_members,
    )


def _active_market_tickers(
    connection: sqlite3.Connection, *, as_of: str, scope_id: str | None = None,
) -> set[str]:
    """Return tickers whose latest known ticker/CIK state is active at the cutoff."""
    if scope_id is None:
        raise ValueError("market refresh requires an explicit request scope")
    member_ids = [row[0] for row in connection.execute(
        "SELECT member_id FROM phase2_universe_members ORDER BY member_id",
    ).fetchall()]
    active_rows = _latest_member_rows(connection, member_ids, as_of=as_of)
    active_pairs = {(str(row["ticker"]), str(row["cik"])) for row in active_rows}
    scope = connection.execute(
        "SELECT scope_id, as_of FROM phase2_refresh_scopes WHERE scope_id = ?",
        (scope_id,),
    ).fetchone()
    if scope is None:
        raise ValueError("market refresh scope does not exist")
    if str(scope["as_of"]) > as_of:
        raise ValueError("market refresh scope is after the requested cutoff")
    scoped_pairs = {
        (str(row["ticker"]), str(row["cik"]))
        for row in connection.execute(
            """SELECT u.ticker, u.cik FROM phase2_refresh_scope_members sm
               JOIN phase2_universe_members u ON u.member_id = sm.member_id
               WHERE sm.scope_id = ?""",
            (scope_id,),
        ).fetchall()
    }
    return {ticker for ticker, cik in active_pairs & scoped_pairs}


def import_sp500_universe(
    connection: sqlite3.Connection, path: Path, *, request_id: str, commit: bool = True,
) -> int:
    """Import an explicit point-in-time or current-only S&P 500 snapshot."""
    _validate_request_id(request_id)
    # Keep commit=False caller-managed even when the connection was idle: an
    # outer transaction is needed because releasing the only SQLite savepoint
    # otherwise commits it immediately.
    if not connection.in_transaction:
        connection.execute("BEGIN")
    savepoint = "phase2_import_sp500_universe"
    connection.execute(f"SAVEPOINT {savepoint}")
    accepted = 0
    member_ids: list[str] = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != UNIVERSE_COLUMNS:
                raise ValueError("Phase 2 universe must use the documented exact columns")
            for row_number, row in enumerate(reader, 2):
                if row.get(None):
                    raise ValueError(f"Phase 2 universe row {row_number} has extra fields")
                values = {name: _require_text(row, name) for name in UNIVERSE_COLUMNS}
                values["ticker"] = values["ticker"].upper()
                if "sp500" not in values["universe_id"].lower() and "s&p 500" not in values["universe_id"].lower():
                    raise ValueError("universe_id must identify the S&P 500 universe")
                if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", values["ticker"]):
                    raise ValueError(f"Phase 2 universe row {row_number} has an invalid ticker")
                if not re.fullmatch(r"\d{10}", values["cik"]):
                    raise ValueError(f"Phase 2 universe row {row_number} must use a 10-digit CIK")
                if values["membership_status"] not in {"active", "inactive"}:
                    raise ValueError("membership_status must be active or inactive")
                if values["exchange"].upper() not in US_EXCHANGES:
                    raise ValueError("Phase 2 universe exchange must identify a supported US listing venue")
                if values["lookahead_bias_status"] not in {"point_in_time", "current_snapshot_only"}:
                    raise ValueError("lookahead_bias_status must be explicit")
                if values["survivorship_bias_status"] not in {"survivorship_risk_disclosed", "not_claimed"}:
                    raise ValueError("survivorship_bias_status must be explicit")
                if contains_control_content(values):
                    raise ValueError("Phase 2 universe contains control content")
                effective = _iso_date(values["effective_date"], "effective_date")
                retrieved = parse_retrieved_at(values["retrieved_at"])
                if retrieved.date() < date.fromisoformat(effective):
                    raise ValueError("universe retrieval cannot precede effective date")
                _validate_public_reference(values["source_url"], "universe source_url")
                member_id = _canonical_id("P2U", values)
                member_ids.append(member_id)
                connection.execute(
                    """INSERT OR IGNORE INTO phase2_universe_members(
                        member_id, universe_id, effective_date, membership_status, ticker, cik,
                        issuer_name, exchange, source_url, source_version, retrieved_at,
                        lookahead_bias_status, survivorship_bias_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (member_id, values["universe_id"], effective, values["membership_status"], values["ticker"],
                     values["cik"], values["issuer_name"], values["exchange"], values["source_url"],
                     values["source_version"], normalize_retrieved_at(values["retrieved_at"]),
                     values["lookahead_bias_status"], values["survivorship_bias_status"]),
                )
                accepted += int(connection.execute("SELECT changes()").fetchone()[0] == 1)
        input_id = _canonical_id("P2I", sorted(member_ids))
        connection.execute(
            "INSERT OR IGNORE INTO phase2_universe_inputs(input_id, member_ids_json) VALUES (?, ?)",
            (input_id, json.dumps(sorted(member_ids), separators=(",", ":"))),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO phase2_universe_input_members(input_id, member_id) VALUES (?, ?)",
            [(input_id, member_id) for member_id in sorted(member_ids)],
        )
    except Exception:
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    if commit:
        connection.commit()
    return accepted


def _fetch_market(provider: MarketProvider | Callable[..., Sequence[MarketObservation]], tickers: Sequence[str], retrieved_at: str) -> Sequence[MarketObservation]:
    fetcher: Any = getattr(provider, "fetch_daily", provider)
    return fetcher(tickers, retrieved_at=retrieved_at)


def _validate_market_provenance(observation: MarketObservation) -> None:
    if observation.currency != "USD":
        raise ValueError("Phase 2 market currency must be USD")
    if not PROVIDER_NAME.fullmatch(observation.provider):
        raise ValueError("market provider identity is malformed")
    if not SOURCE_VERSION.fullmatch(observation.source_version):
        raise ValueError("market source version is malformed")
    if not observation.provider_observation_id.strip():
        raise ValueError("market provider observation identity is required")
    _validate_public_reference(observation.citation, "market citation", allow_fixture=True)
    if observation.units is not None:
        expected = {field: ("shares" if field == "volume" else "USD_per_share") for field in MARKET_FIELDS}
        if dict(observation.units) != expected:
            raise ValueError("market units do not match the Phase 2 OHLCV contract")
    if contains_control_content({
        "provider": observation.provider, "provider_observation_id": observation.provider_observation_id,
        "source_version": observation.source_version, "citation": observation.citation,
    }):
        raise ValueError("market provenance contains control content")


def _market_freshness(observation: MarketObservation) -> str:
    retrieved = parse_retrieved_at(observation.retrieved_at)
    observed = date.fromisoformat(observation.observation_date)
    age_days = (retrieved.date() - observed).days
    if observation.freshness_status in {"current", "observed"} and age_days > 30:
        return "stale"
    return observation.freshness_status


def _normalized_market_values(observation: MarketObservation) -> dict[str, float]:
    if not isinstance(observation.ticker, str) or not observation.ticker.strip():
        raise ValueError("market ticker is required")
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", observation.ticker.upper()):
        raise ValueError("market ticker is not a typed US symbol")
    _iso_date(observation.observation_date, "market observation_date")
    retrieved = parse_retrieved_at(observation.retrieved_at)
    if retrieved.date() < date.fromisoformat(observation.observation_date):
        raise ValueError("market retrieval cannot precede observation date")
    if observation.freshness_status not in FRESHNESS_STATUSES:
        raise ValueError("market freshness status is unsupported")
    _validate_market_provenance(observation)
    result: dict[str, float] = {}
    for field in MARKET_FIELDS:
        if field not in observation.values:
            raise ValueError(f"market observation is missing {field}")
        if isinstance(observation.values[field], bool):
            raise ValueError(f"market {field} must not be boolean")
        try:
            numeric = float(observation.values[field])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"market {field} is not numeric") from exc
        if not math.isfinite(numeric) or (field != "volume" and numeric <= 0) or (field == "volume" and numeric < 0):
            raise ValueError(f"market {field} is not a valid finite value")
        result[field] = numeric
    if result["high"] < max(result["open"], result["close"]) or result["low"] > min(result["open"], result["close"]):
        raise ValueError("market OHLC values are inconsistent")
    return result


def refresh_market_observations(
    connection: sqlite3.Connection, provider: MarketProvider | Callable[..., Sequence[MarketObservation]],
    tickers: Sequence[str], *, request_id: str, retrieved_at: str, commit: bool = True,
    sensitive_values: Sequence[str] = (), usable_result: list[bool] | None = None,
    as_of: str | None = None, scope_id: str | None = None,
) -> int:
    """Fetch, validate, and idempotently store typed daily market observations."""
    _validate_request_id(request_id)
    requested_tickers: set[str] = set()
    for ticker in tickers:
        if not isinstance(ticker, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker.upper()):
            raise ValueError("market requests require typed US ticker symbols")
        requested_tickers.add(ticker.upper())
    normalized_retrieved = normalize_retrieved_at(retrieved_at)
    cutoff = None if as_of is None else _iso_date(as_of, "as_of")
    if scope_id is not None:
        cutoff = _immutable_scope_cutoff(
            connection, scope_id=scope_id, requested_cutoff=cutoff, operation="market",
        )
    else:
        cutoff = cutoff or normalized_retrieved[:10]
    membership_cutoff = cutoff
    provider_name = type(provider).__name__.lower()
    if scope_id is None:
        exact_member_ids = _resolve_exact_target_member_ids(
            connection,
            [(ticker, None) for ticker in sorted(requested_tickers)],
            as_of=membership_cutoff,
        )
        scope_id = _ensure_request_scope(
            connection, request_id=request_id, as_of=membership_cutoff,
            member_ids=exact_member_ids,
        )
    _validate_scope_request(connection, request_id=request_id, scope_id=scope_id)
    imported_tickers = _active_market_tickers(connection, as_of=membership_cutoff, scope_id=scope_id)
    eligible_tickers = requested_tickers & imported_tickers
    for ticker in sorted(requested_tickers - eligible_tickers):
        _record_failure(
            connection, request_id=request_id, source_name=provider_name,
            failure_code="market_ticker_not_in_universe", message="market ticker is not an active imported S&P 500 member",
            ticker=ticker, observed_at=normalized_retrieved, scope_id=scope_id,
        )
    requested_tickers = eligible_tickers
    if not requested_tickers:
        if usable_result is not None:
            usable_result.append(False)
        if commit:
            connection.commit()
        return 0
    try:
        observations = _fetch_market(provider, sorted(requested_tickers), normalized_retrieved)
        if not isinstance(observations, Sequence):
            raise ValueError("provider response must be a sequence of observations")
    except ProviderUnavailable as exc:
        _record_failure(connection, request_id=request_id, source_name=provider_name, failure_code="provider_unavailable", message=str(exc), observed_at=normalized_retrieved, sensitive_values=sensitive_values, scope_id=scope_id)
        if commit:
            connection.commit()
        if usable_result is not None:
            usable_result.append(False)
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _record_failure(connection, request_id=request_id, source_name=provider_name, failure_code="invalid_provider_response", message=str(exc), observed_at=normalized_retrieved, sensitive_values=sensitive_values, scope_id=scope_id)
        if commit:
            connection.commit()
        if usable_result is not None:
            usable_result.append(False)
        return 0
    returned_tickers = {
        observation.ticker.upper()
        for observation in observations
        if isinstance(observation, MarketObservation) and isinstance(observation.ticker, str)
    }

    validated_market_rows: list[tuple[MarketObservation, dict[str, float], str, list[tuple[str, str, str, str]]]] = []
    invalid_observation: MarketObservation | object | None = None
    try:
        for observation in observations:
            invalid_observation = observation
            if not isinstance(observation, MarketObservation):
                raise ValueError("provider response contained a non-observation record")
            if observation.ticker.upper() not in requested_tickers:
                raise ValueError("provider response contained an unrequested ticker")
            values = _normalized_market_values(observation)
            freshness_status = _market_freshness(observation)
            if cutoff is not None and (
                observation.observation_date > cutoff
                or normalize_retrieved_at(observation.retrieved_at)[:10] > cutoff
            ):
                raise ValueError("market observation is after the requested analysis cutoff")
            identities: list[tuple[str, str, str, str]] = []
            for field, value in values.items():
                identities.append((field, *canonical_market_observation(
                    ticker=observation.ticker.upper(), observation_date=observation.observation_date,
                    provider=observation.provider, provider_observation_id=observation.provider_observation_id,
                    field=field, value=value, currency=observation.currency,
                    citation=observation.citation, source_version=observation.source_version,
                    freshness_status=freshness_status,
                )))
            validated_market_rows.append((observation, values, freshness_status, identities))
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        safe_ticker_value = getattr(invalid_observation, "ticker", None)
        safe_ticker = (
            safe_ticker_value.upper()
            if isinstance(safe_ticker_value, str)
            and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", safe_ticker_value.upper())
            else None
        )
        _record_failure(
            connection, request_id=request_id, source_name=provider_name,
            failure_code="invalid_market_observation",
            message="provider response contained an invalid market observation",
            ticker=safe_ticker, observed_at=normalized_retrieved, scope_id=scope_id,
        )
        if usable_result is not None:
            usable_result.append(False)
        if commit:
            connection.commit()
        return 0

    for ticker in sorted(requested_tickers - returned_tickers):
        _record_failure(connection, request_id=request_id, source_name=provider_name, failure_code="market_observation_unavailable", message="provider returned no daily observation", ticker=ticker, observed_at=normalized_retrieved, scope_id=scope_id)

    if not connection.in_transaction:
        connection.execute("BEGIN")
    savepoint = "phase2_market_batch"
    connection.execute(f"SAVEPOINT {savepoint}")
    accepted = 0
    binding_added = False
    batch_observation_ids: set[str] = set()
    response_observation_ids: list[str] = []
    try:
        for observation, values, freshness_status, identities in validated_market_rows:
            for field, observation_identity, observation_hash, observation_id in identities:
                value = values[field]
                response_observation_ids.append(observation_id)
                connection.execute(
                    """INSERT OR IGNORE INTO phase2_market_observations(
                        observation_id, observation_identity, observation_hash, conflict_status,
                        ticker, observation_date, field, value, unit, currency, provider,
                        provider_observation_id, source_version, citation, retrieved_at, freshness_status
                    ) VALUES (?, ?, ?, 'usable', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (observation_id, observation_identity, observation_hash,
                     observation.ticker.upper(), observation.observation_date, field, str(value),
                     "shares" if field == "volume" else "USD_per_share", observation.currency,
                     observation.provider, observation.provider_observation_id, observation.source_version,
                     observation.citation, normalize_retrieved_at(observation.retrieved_at), freshness_status),
                )
                accepted += int(connection.execute("SELECT changes()").fetchone()[0] == 1)
                connection.execute(
                    """INSERT OR IGNORE INTO phase2_market_observation_bindings(
                        request_id, scope_id, observation_id
                    ) VALUES (?, ?, ?)""",
                    (request_id, scope_id, observation_id),
                )
                binding_was_added = connection.execute("SELECT changes()").fetchone()[0] == 1
                binding_added = binding_added or binding_was_added
                if binding_was_added:
                    batch_observation_ids.add(observation_id)
            if freshness_status in {"stale", "unavailable"}:
                _record_failure(connection, request_id=request_id, source_name=observation.provider, failure_code="stale_market_observation" if freshness_status == "stale" else "market_observation_unavailable", message=f"market observation freshness is {freshness_status}", ticker=observation.ticker.upper(), observed_at=normalized_retrieved, scope_id=scope_id)
        if binding_added:
            _detect_market_conflicts(
                connection, request_id=request_id, scope_id=scope_id,
                observation_ids=tuple(sorted(batch_observation_ids)),
            )
        if usable_result is not None:
            if response_observation_ids:
                placeholders = ",".join("?" for _ in response_observation_ids)
                usable_parameters: tuple[object, ...] = (
                    *response_observation_ids, cutoff or "9999-12-31", cutoff or "9999-12-31",
                )
                usable_result.append(bool(connection.execute(
                    f"SELECT 1 FROM phase2_market_observations WHERE observation_id IN ({placeholders}) "
                    "AND freshness_status IN ('current', 'observed') "
                    "AND observation_date <= ? AND substr(retrieved_at, 1, 10) <= ? "
                    + "AND EXISTS (SELECT 1 FROM phase2_market_observation_bindings b WHERE b.request_id = ? AND b.scope_id = ? AND b.observation_id = phase2_market_observations.observation_id AND b.conflict_status = 'usable') "
                    + "LIMIT 1",
                    (*usable_parameters, request_id, scope_id),
                ).fetchone()))
            else:
                usable_result.append(False)
    except (sqlite3.Error, AttributeError, TypeError, ValueError, KeyError):
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        _record_failure(
            connection, request_id=request_id, source_name=provider_name,
            failure_code="market_batch_failed",
            message="market observation batch was rolled back after a persistence failure",
            observed_at=normalized_retrieved, scope_id=scope_id,
        )
        if usable_result is not None:
            usable_result.append(False)
        if commit:
            connection.commit()
        return 0
    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    if commit:
        connection.commit()
    return accepted


def _detect_market_conflicts(
    connection: sqlite3.Connection, *, request_id: str, scope_id: str | None = None,
    observation_ids: Sequence[str] = (),
) -> None:
    scope_id = scope_id or _request_scope_id(connection, request_id=request_id)
    if scope_id is None:
        raise ValueError("market conflict discovery requires an explicit request scope")
    _validate_scope_request(connection, request_id=request_id, scope_id=scope_id)
    normalized_observation_ids = tuple(sorted(set(observation_ids)))
    if not normalized_observation_ids:
        return
    scope_cutoff = connection.execute(
        "SELECT as_of FROM phase2_refresh_scopes WHERE scope_id = ?", (scope_id,)
    ).fetchone()["as_of"]
    id_placeholders = ",".join("?" for _ in normalized_observation_ids)
    touched_keys = connection.execute(
        """SELECT DISTINCT m.ticker, m.observation_date, m.field, m.currency
           FROM phase2_market_observations m
           WHERE m.observation_id IN ({id_placeholders})
             AND m.observation_date <= ?
             AND substr(m.retrieved_at, 1, 10) <= ?
           ORDER BY m.ticker, m.observation_date, m.field, m.currency""".format(
               id_placeholders=id_placeholders,
           ),
        (*normalized_observation_ids, scope_cutoff, scope_cutoff),
    ).fetchall()
    for key in touched_keys:
        row = connection.execute(
            """SELECT ticker, observation_date, field, currency, COUNT(DISTINCT value) AS variants
               FROM phase2_market_observations
               WHERE ticker = ? AND observation_date = ? AND field = ? AND currency = ?
                 AND observation_date <= ? AND substr(retrieved_at, 1, 10) <= ?
               GROUP BY ticker, observation_date, field, currency""",
            (key["ticker"], key["observation_date"], key["field"], key["currency"], scope_cutoff, scope_cutoff),
        ).fetchone()
        if row is None or row["variants"] <= 1:
            continue
        connection.execute(
            """UPDATE phase2_market_observations SET conflict_status = 'conflict'
               WHERE ticker = ? AND observation_date = ? AND field = ? AND currency = ?
                 AND observation_date <= ? AND substr(retrieved_at, 1, 10) <= ?""",
            (row["ticker"], row["observation_date"], row["field"], row["currency"], scope_cutoff, scope_cutoff),
        )
        connection.execute(
            """UPDATE phase2_market_observation_bindings SET conflict_status = 'conflict'
               WHERE request_id = ? AND scope_id = ? AND observation_id IN (
                   SELECT observation_id FROM phase2_market_observations
                   WHERE ticker = ? AND observation_date = ? AND field = ? AND currency = ?
                     AND observation_date <= ? AND substr(retrieved_at, 1, 10) <= ?
               )""",
            (request_id, scope_id, row["ticker"], row["observation_date"], row["field"], row["currency"], scope_cutoff, scope_cutoff),
        )
        _record_failure(connection, request_id=request_id, source_name="phase2-market", failure_code="source_conflict", message=f"conflicting {row['field']} values/provenance for {row['ticker']} on {row['observation_date']}", ticker=row["ticker"], observed_at=row["observation_date"], scope_id=scope_id)
    touched_identities = connection.execute(
        """SELECT DISTINCT m.observation_identity
           FROM phase2_market_observations m
           WHERE m.observation_id IN ({id_placeholders})
             AND m.observation_date <= ?
             AND substr(m.retrieved_at, 1, 10) <= ?
           ORDER BY m.observation_identity""".format(
               id_placeholders=id_placeholders,
           ),
        (*normalized_observation_ids, scope_cutoff, scope_cutoff),
    ).fetchall()
    for identity in touched_identities:
        row = connection.execute(
            """SELECT observation_identity, ticker, observation_date, field, COUNT(*) AS variants
               FROM phase2_market_observations
               WHERE observation_identity = ?
                 AND observation_date <= ? AND substr(retrieved_at, 1, 10) <= ?
               GROUP BY observation_identity""",
            (identity["observation_identity"], scope_cutoff, scope_cutoff),
        ).fetchone()
        if row is None or row["variants"] <= 1:
            continue
        connection.execute(
            """UPDATE phase2_market_observations SET conflict_status = 'conflict'
               WHERE observation_identity = ?
                 AND observation_date <= ? AND substr(retrieved_at, 1, 10) <= ?""",
            (row["observation_identity"], scope_cutoff, scope_cutoff),
        )
        connection.execute(
            """UPDATE phase2_market_observation_bindings SET conflict_status = 'conflict'
               WHERE request_id = ? AND scope_id = ? AND observation_id IN (
                   SELECT observation_id FROM phase2_market_observations
                   WHERE observation_identity = ?
                     AND observation_date <= ? AND substr(retrieved_at, 1, 10) <= ?
               )""",
            (request_id, scope_id, row["observation_identity"], scope_cutoff, scope_cutoff),
        )
        _record_failure(connection, request_id=request_id, source_name="phase2-market", failure_code="source_conflict", message=f"changed market provenance for {row['ticker']} on {row['observation_date']}", ticker=row["ticker"], observed_at=row["observation_date"], scope_id=scope_id)


def _validate_fact(record: EvidenceRecord) -> None:
    if record.fact_value is None:
        return
    if isinstance(record.fact_value, bool):
        raise ValueError("evidence fact value must not be boolean")
    if record.fact_field in TEXT_FACT_FIELDS:
        if record.fact_value not in SUPPORTED_SEC_FORMS:
            raise ValueError("textual evidence form is unsupported")
        return
    if not record.fact_unit or not record.fact_unit.strip():
        raise ValueError("numeric evidence fact unit is required")
    try:
        fact_numeric = float(record.fact_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("numeric evidence fact is not numeric") from exc
    if not math.isfinite(fact_numeric):
        raise ValueError("numeric evidence fact must be finite")


def _validate_universe_target(
    connection: sqlite3.Connection, ticker: str, cik: str | None, *, as_of: str | None = None,
) -> None:
    clauses = ["ticker = ?"]
    parameters: list[object] = [ticker]
    if cik is not None:
        clauses.append("cik = ?")
        parameters.append(cik)
    if as_of is not None:
        clauses.extend(["effective_date <= ?", "substr(retrieved_at, 1, 10) <= ?"])
        parameters.extend([as_of, as_of])
    rows = connection.execute(
        "SELECT ticker, cik, membership_status, effective_date, retrieved_at, member_id "
        f"FROM phase2_universe_members WHERE {' AND '.join(clauses)} "
        "ORDER BY ticker, cik, effective_date DESC, retrieved_at DESC, member_id DESC",
        parameters,
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest.setdefault(row["cik"], row)
    if not any(row["membership_status"] == "active" for row in latest.values()):
        raise ValueError("evidence target is not an imported S&P 500 universe mapping")


def _validate_scoped_evidence_target(
    connection: sqlite3.Connection, ticker: str, cik: str | None, *, scope_id: str,
) -> None:
    scope = connection.execute(
        "SELECT as_of FROM phase2_refresh_scopes WHERE scope_id = ?", (scope_id,)
    ).fetchone()
    if scope is None:
        raise ValueError("evidence target refresh scope does not exist")
    member_ids = [row[0] for row in connection.execute(
        "SELECT member_id FROM phase2_refresh_scope_members WHERE scope_id = ? ORDER BY member_id",
        (scope_id,),
    ).fetchall()]
    members = _latest_member_rows(connection, member_ids, as_of=str(scope["as_of"]))
    if not any(
        row["ticker"] == ticker and (cik is None or row["cik"] == cik)
        for row in members
    ):
        raise ValueError("evidence target is not in the requested refresh scope")


def _validate_evidence_record(
    connection: sqlite3.Connection, record: EvidenceRecord, *, as_of: str | None = None,
    scope_id: str | None = None,
) -> tuple[str | None, str | None, str | None, str, bool]:
    if record.evidence_kind not in {"filing", "news", "alert"}:
        raise ValueError("unsupported Phase 2 evidence kind")
    required = {
        "evidence_identity": record.evidence_identity, "evidence_subtype": record.evidence_subtype,
        "source_name": record.source_name, "source_identifier": record.source_identifier,
        "source_url": record.source_url, "topic": record.topic, "source_quality": record.source_quality,
        "recency_status": record.recency_status, "corroboration_status": record.corroboration_status,
        "uncertainty_status": record.uncertainty_status, "source_version": record.source_version,
        "citation": record.citation, "retrieved_at": record.retrieved_at,
    }
    if any(not isinstance(value, str) or not value.strip() for value in required.values()):
        raise ValueError("evidence provenance is incomplete")
    if contains_control_content(asdict(record)):
        raise ValueError("evidence record contains control content")
    if record.topic.upper() in {"BUY", "HOLD", "SELL", "BUY CANDIDATE", "SELL CANDIDATE"} or record.evidence_subtype.upper() in {"BUY", "HOLD", "SELL"}:
        raise ValueError("evidence contains a directional verdict")
    if record.recency_status not in RECENCY_STATUSES:
        raise ValueError("evidence recency status is unsupported")
    _validate_public_reference(record.source_url, "evidence source_url")
    _validate_public_reference(record.citation, "evidence citation", allow_fixture=True)
    if contains_control_content(record.metadata):
        raise ValueError("evidence metadata contains control content")
    if contains_control_content(record.citation):
        raise ValueError("evidence citation contains control content")
    ticker = record.ticker.upper() if isinstance(record.ticker, str) and record.ticker.strip() else None
    issuer_cik = record.issuer_cik
    if ticker is not None and not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
        raise ValueError("evidence ticker is not a typed US symbol")
    if issuer_cik is not None:
        if not isinstance(issuer_cik, str) or not re.fullmatch(r"\d{1,10}", issuer_cik):
            raise ValueError("evidence issuer CIK is not typed")
        issuer_cik = issuer_cik.zfill(10)
    if ticker is None:
        raise ValueError("Phase 2 evidence requires an explicit ticker mapping")
    _validate_universe_target(connection, ticker, issuer_cik, as_of=as_of)
    if scope_id is not None:
        _validate_scoped_evidence_target(connection, ticker, issuer_cik, scope_id=scope_id)
    for label, value in (("period_start", record.period_start), ("period_end", record.period_end)):
        if value is not None:
            _iso_date(value, label)
    if record.period_start and record.period_end and record.period_start > record.period_end:
        raise ValueError("evidence period starts after it ends")
    _validate_fact(record)
    retrieved = normalize_retrieved_at(record.retrieved_at)
    publication = None
    if record.publication_at is not None:
        publication = normalize_retrieved_at(record.publication_at)
        if parse_retrieved_at(retrieved) < parse_retrieved_at(publication):
            raise ValueError("evidence retrieval cannot precede publication")
        if record.period_end and date.fromisoformat(record.period_end) > date.fromisoformat(publication[:10]):
            raise ValueError("evidence period cannot follow publication")
    within_scope_cutoff = not (
        as_of is not None
        and (
            retrieved[:10] > as_of
            or (publication is not None and publication[:10] > as_of)
        )
    )
    if not within_scope_cutoff:
        raise _EvidenceAfterScopeCutoff(
            "evidence publication or retrieval is after the immutable scope cutoff"
        )
    effective_recency = record.recency_status
    if publication is not None and record.recency_status in {"recent", "old"}:
        effective_recency = "recent" if (parse_retrieved_at(retrieved) - parse_retrieved_at(publication)).days <= 30 else "old"
    return ticker, issuer_cik, publication, effective_recency, within_scope_cutoff


def ingest_evidence(
    connection: sqlite3.Connection, records: Sequence[EvidenceRecord], *, request_id: str,
    commit: bool = True, as_of: str | None = None, usable_result: list[bool] | None = None,
    scope_id: str | None = None,
) -> int:
    _validate_request_id(request_id)
    records = tuple(records)
    if as_of is None:
        if scope_id is not None:
            direct_cutoff = _immutable_scope_cutoff(
                connection, scope_id=scope_id, requested_cutoff=None, operation="evidence",
            )
        else:
            record_dates: list[str] = []
            for record in records:
                try:
                    record_dates.append(normalize_retrieved_at(record.retrieved_at)[:10])
                except (AttributeError, TypeError, ValueError):
                    continue
            direct_cutoff = max(record_dates, default=utc_now()[:10])
    else:
        direct_cutoff = _iso_date(as_of, "as_of")
        if scope_id is not None:
            direct_cutoff = _immutable_scope_cutoff(
                connection, scope_id=scope_id, requested_cutoff=direct_cutoff, operation="evidence",
            )
    if scope_id is None:
        scope_id = _ensure_request_scope(
            connection, request_id=request_id, as_of=direct_cutoff,
            member_ids=_direct_evidence_target_member_ids(connection, records, as_of=direct_cutoff),
        )
    _validate_scope_request(connection, request_id=request_id, scope_id=scope_id)
    as_of = direct_cutoff
    if not connection.in_transaction:
        connection.execute("BEGIN")
    savepoint = "phase2_ingest_evidence"
    connection.execute(f"SAVEPOINT {savepoint}")
    accepted = 0
    binding_added = False
    batch_evidence_ids: set[str] = set()
    invalid_record: EvidenceRecord | None = None
    response_evidence_ids: list[str] = []
    try:
        for record in records:
            invalid_record = record
            ticker, issuer_cik, publication, effective_recency, within_scope_cutoff = _validate_evidence_record(
                connection, record, as_of=as_of, scope_id=scope_id,
            )
            payload = asdict(record)
            record_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            evidence_id = f"P2E-{record_hash[:24]}"
            response_evidence_ids.append(evidence_id)
            connection.execute(
                """INSERT OR IGNORE INTO phase2_evidence(
                    evidence_id, evidence_identity, record_hash, conflict_status, evidence_kind,
                    evidence_subtype, source_name, source_identifier, source_url, ticker, issuer_cik,
                    publication_at, period_start, period_end, fact_field, fact_value, fact_unit, topic,
                    evidence_text, source_quality, recency_status, corroboration_status, uncertainty_status,
                    metadata_json, retrieved_at, source_version, citation
                ) VALUES (?, ?, ?, 'usable', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (evidence_id, record.evidence_identity, record_hash, record.evidence_kind, record.evidence_subtype,
                 record.source_name, record.source_identifier, record.source_url, ticker, issuer_cik, publication,
                 record.period_start, record.period_end, record.fact_field, record.fact_value, record.fact_unit,
                 record.topic, record.evidence_text, record.source_quality, effective_recency,
                 record.corroboration_status, record.uncertainty_status, json.dumps(record.metadata, sort_keys=True, allow_nan=False),
                 normalize_retrieved_at(record.retrieved_at), record.source_version, record.citation),
            )
            accepted += int(connection.execute("SELECT changes()").fetchone()[0] == 1)
            connection.execute(
                """INSERT OR IGNORE INTO phase2_evidence_bindings(
                    request_id, scope_id, evidence_id, conflict_status
                ) VALUES (?, ?, ?, 'usable')""",
                (request_id, scope_id, evidence_id),
            )
            binding_was_added = connection.execute("SELECT changes()").fetchone()[0] == 1
            binding_added = binding_added or binding_was_added
            if binding_was_added:
                batch_evidence_ids.add(evidence_id)
            if effective_recency in {"stale", "unknown"}:
                _record_failure(
                    connection, request_id=request_id, source_name=record.source_name,
                    failure_code="unusable_evidence_recency", message=f"evidence recency is {effective_recency}",
                    ticker=ticker, observed_at=record.retrieved_at, scope_id=scope_id,
                )
        if binding_added:
            _detect_evidence_conflicts(
                connection, request_id=request_id, scope_id=scope_id,
                evidence_ids=tuple(sorted(batch_evidence_ids)),
            )
        if usable_result is not None:
            if response_evidence_ids:
                placeholders = ",".join("?" for _ in response_evidence_ids)
                usable_parameters = (
                    *response_evidence_ids, as_of or "9999-12-31", as_of or "9999-12-31",
                )
                usable_result.append(bool(connection.execute(
                    f"SELECT 1 FROM phase2_evidence WHERE evidence_id IN ({placeholders}) "
                    "AND recency_status IN ('recent', 'old') "
                    "AND (publication_at IS NULL OR substr(publication_at, 1, 10) <= ?) "
                    "AND substr(retrieved_at, 1, 10) <= ? "
                    + "AND EXISTS (SELECT 1 FROM phase2_evidence_bindings b WHERE b.request_id = ? AND b.scope_id = ? AND b.evidence_id = phase2_evidence.evidence_id AND b.conflict_status = 'usable') "
                    + "LIMIT 1",
                    (*usable_parameters, request_id, scope_id),
                ).fetchone()))
            else:
                usable_result.append(False)
    except _EvidenceAfterScopeCutoff as exc:
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        _record_failure(
            connection, request_id=request_id,
            source_name=getattr(invalid_record, "source_name", "phase2-evidence"),
            failure_code="evidence_after_scope_cutoff", message=str(exc),
            ticker=getattr(invalid_record, "ticker", None),
            observed_at=getattr(invalid_record, "retrieved_at", None), scope_id=scope_id,
        )
        if usable_result is not None:
            usable_result.append(False)
        if commit:
            connection.commit()
        raise
    except (AttributeError, TypeError, ValueError, sqlite3.Error) as exc:
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        _record_failure(
            connection, request_id=request_id, source_name="phase2-evidence",
            failure_code="invalid_evidence_record", message=str(exc),
            ticker=getattr(invalid_record, "ticker", None), observed_at=None, scope_id=scope_id,
        )
        if usable_result is not None:
            usable_result.append(False)
        if commit:
            connection.commit()
        raise
    else:
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        if commit:
            connection.commit()
        return accepted


def _detect_evidence_conflicts(
    connection: sqlite3.Connection, *, request_id: str, scope_id: str | None = None,
    evidence_ids: Sequence[str] = (),
) -> None:
    scope_id = scope_id or _request_scope_id(connection, request_id=request_id)
    if scope_id is None:
        raise ValueError("evidence conflict discovery requires an explicit request scope")
    _validate_scope_request(connection, request_id=request_id, scope_id=scope_id)
    normalized_evidence_ids = tuple(sorted(set(evidence_ids)))
    if not normalized_evidence_ids:
        return
    scope_cutoff = connection.execute(
        "SELECT as_of FROM phase2_refresh_scopes WHERE scope_id = ?", (scope_id,)
    ).fetchone()["as_of"]
    id_placeholders = ",".join("?" for _ in normalized_evidence_ids)
    touched_identities = connection.execute(
        """SELECT DISTINCT e.evidence_identity
           FROM phase2_evidence e
           WHERE e.evidence_id IN ({id_placeholders})
             AND substr(e.retrieved_at, 1, 10) <= ?
             AND (e.publication_at IS NULL OR substr(e.publication_at, 1, 10) <= ?)
           ORDER BY e.evidence_identity""".format(
               id_placeholders=id_placeholders,
           ),
        (*normalized_evidence_ids, scope_cutoff, scope_cutoff),
    ).fetchall()
    for identity in touched_identities:
        row = connection.execute(
            """SELECT evidence_identity, ticker, evidence_kind, evidence_subtype,
                      publication_at, period_end, fact_field, COUNT(*) AS variants
               FROM phase2_evidence
               WHERE evidence_identity = ?
                 AND substr(retrieved_at, 1, 10) <= ?
                 AND (publication_at IS NULL OR substr(publication_at, 1, 10) <= ?)
               GROUP BY evidence_identity""",
            (identity["evidence_identity"], scope_cutoff, scope_cutoff),
        ).fetchone()
        if row is None or row["variants"] <= 1:
            continue
        connection.execute(
            """UPDATE phase2_evidence SET conflict_status = 'conflict'
               WHERE evidence_identity = ?
                 AND substr(retrieved_at, 1, 10) <= ?
                 AND (publication_at IS NULL OR substr(publication_at, 1, 10) <= ?)""",
            (row["evidence_identity"], scope_cutoff, scope_cutoff),
        )
        connection.execute(
            """UPDATE phase2_evidence_bindings SET conflict_status = 'conflict'
               WHERE request_id = ? AND scope_id = ? AND evidence_id IN (
                   SELECT evidence_id FROM phase2_evidence
                   WHERE evidence_identity = ?
                     AND substr(retrieved_at, 1, 10) <= ?
                     AND (publication_at IS NULL OR substr(publication_at, 1, 10) <= ?)
               )""",
            (request_id, scope_id, row["evidence_identity"], scope_cutoff, scope_cutoff),
        )
        _record_failure(connection, request_id=request_id, source_name="phase2-evidence", failure_code="evidence_conflict", message=f"changed value/provenance variants for {row['evidence_identity']}", ticker=row["ticker"], observed_at=row["period_end"] or row["publication_at"], scope_id=scope_id)


def _json_safe_rows(rows: Sequence[sqlite3.Row]) -> list[dict[str, object]]:
    return [dict(row) for row in rows]


def _persist_refresh_scope(
    connection: sqlite3.Connection, *, request_id: str, as_of: str,
    member_ids: Sequence[str],
) -> str:
    """Persist or validate one immutable request-to-scope binding."""
    _validate_request_id(request_id)
    normalized_members = sorted(set(member_ids))
    existing_scope_id = _request_scope_id(connection, request_id=request_id)
    if existing_scope_id is not None:
        scope = connection.execute(
            "SELECT scope_id, request_id, as_of FROM phase2_refresh_scopes WHERE scope_id = ?",
            (existing_scope_id,),
        ).fetchone()
        if scope is None or scope["request_id"] != request_id:
            raise _RequestScopeConflict("Phase 2 request scope binding is invalid")
        if scope["as_of"] != as_of:
            raise _RequestScopeConflict("request_id is already bound to a different as_of cutoff")
        existing_members = sorted(
            str(row[0]) for row in connection.execute(
                "SELECT member_id FROM phase2_refresh_scope_members WHERE scope_id = ? ORDER BY member_id",
                (existing_scope_id,),
            ).fetchall()
        )
        if existing_members != normalized_members:
            raise _RequestScopeConflict("Phase 2 request scope membership does not match the replay")
        return str(existing_scope_id)

    scope_id = _canonical_id("P2Q", [request_id, as_of, normalized_members])
    existing_scope = connection.execute(
        "SELECT request_id, as_of FROM phase2_refresh_scopes WHERE scope_id = ?", (scope_id,)
    ).fetchone()
    if existing_scope is not None and (
        existing_scope["request_id"] != request_id or existing_scope["as_of"] != as_of
    ):
        raise _RequestScopeConflict("Phase 2 refresh scope identity is already bound")
    connection.execute(
        "INSERT OR IGNORE INTO phase2_refresh_scopes(scope_id, request_id, as_of) VALUES (?, ?, ?)",
        (scope_id, request_id, as_of),
    )
    connection.executemany(
        "INSERT OR IGNORE INTO phase2_refresh_scope_members(scope_id, member_id) VALUES (?, ?)",
        [(scope_id, member_id) for member_id in normalized_members],
    )
    connection.execute(
        "INSERT OR IGNORE INTO phase2_refresh_run_scopes(request_id, scope_id) VALUES (?, ?)",
        (request_id, scope_id),
    )
    bound_scope_id = _request_scope_id(connection, request_id=request_id)
    if bound_scope_id != scope_id:
        raise _RequestScopeConflict("Phase 2 request scope membership does not match the replay")
    return str(bound_scope_id)


def _report_scope_id(
    connection: sqlite3.Connection, *, cutoff: str, requested_scope_id: str | None,
    request_id: str | None = None,
) -> str:
    if request_id is not None:
        _validate_request_id(request_id)
    if requested_scope_id is not None:
        row = connection.execute(
            "SELECT scope_id, request_id, as_of FROM phase2_refresh_scopes WHERE scope_id = ?",
            (requested_scope_id,),
        ).fetchone()
        if row is None or row["as_of"] > cutoff:
            raise ValueError("Phase 2 report scope is missing or after the requested cutoff")
        if request_id is not None and row["request_id"] != request_id:
            raise ValueError("Phase 2 report scope is bound to a different request")
        return str(row["scope_id"])
    if request_id is not None:
        row = connection.execute(
            """SELECT s.scope_id FROM phase2_refresh_scopes s
               JOIN phase2_refresh_run_scopes rs ON rs.scope_id = s.scope_id
               WHERE rs.request_id = ? AND s.as_of <= ?""",
            (request_id, cutoff),
        ).fetchone()
        if row is None:
            raise ValueError("Phase 2 report request has no refresh scope")
        return str(row["scope_id"])
    row = connection.execute(
        """SELECT s.scope_id FROM phase2_refresh_scopes s
           JOIN phase2_refresh_run_scopes rs ON rs.scope_id = s.scope_id
           JOIN phase2_refresh_runs r ON r.request_id = rs.request_id
           WHERE s.as_of <= ? AND r.status != 'failed'
           ORDER BY s.as_of DESC, r.rowid DESC, r.run_id DESC LIMIT 1""",
        (cutoff,),
    ).fetchone()
    if row is not None:
        return str(row["scope_id"])
    direct_scopes = connection.execute(
        """SELECT s.scope_id FROM phase2_refresh_scopes s
           JOIN phase2_refresh_run_scopes rs ON rs.scope_id = s.scope_id
           WHERE s.as_of <= ?
             AND NOT EXISTS (
                 SELECT 1 FROM phase2_refresh_runs r WHERE r.request_id = rs.request_id
             )
           ORDER BY s.scope_id""",
        (cutoff,),
    ).fetchall()
    if len(direct_scopes) == 1:
        return str(direct_scopes[0]["scope_id"])
    if direct_scopes:
        raise ValueError("Phase 2 report requires an explicit request_id or scope_id")
    raise ValueError("Phase 2 report has no unambiguous request-bound scope")


def _assert_evidence_only(report: Mapping[str, object]) -> None:
    for collection_name in ("universe", "market_observations", "evidence", "conflicts", "market_conflicts", "failures"):
        collection = report.get(collection_name, [])
        if isinstance(collection, list):
            for row in collection:
                if isinstance(row, Mapping):
                    metadata = row.get("metadata_json")
                    if isinstance(metadata, str):
                        try:
                            parsed = json.loads(metadata)
                        except json.JSONDecodeError:
                            raise ValueError("Phase 2 metadata is not valid JSON")
                        if contains_control_content(parsed):
                            raise ValueError("Phase 2 evidence output contains control content")
    if contains_control_content(report):
        raise ValueError("Phase 2 evidence output contains control content")


def phase2_evidence_report(
    connection: sqlite3.Connection, *, as_of: str, commit: bool = True,
    scope_id: str | None = None, request_id: str | None = None,
) -> dict[str, object]:
    cutoff = _iso_date(as_of, "as_of")
    if request_id is not None:
        _validate_request_id(request_id)
    selected_scope_id = _report_scope_id(
        connection, cutoff=cutoff, requested_scope_id=scope_id, request_id=request_id,
    )
    scope_row = connection.execute(
        "SELECT request_id, as_of FROM phase2_refresh_scopes WHERE scope_id = ?",
        (selected_scope_id,),
    ).fetchone()
    if scope_row is not None and str(scope_row["as_of"]) > cutoff:
        raise ValueError("Phase 2 report scope is after the requested cutoff")
    if scope_row is not None:
        cutoff = min(cutoff, str(scope_row["as_of"]))
    bound_request_id = request_id or (None if scope_row is None else str(scope_row["request_id"]))
    if bound_request_id is None:
        raise ValueError("Phase 2 report scope has no request binding")
    market_binding = """AND EXISTS (
             SELECT 1 FROM phase2_market_observation_bindings b
             WHERE b.observation_id = m.observation_id
               AND b.request_id = ? AND b.scope_id = ?
               AND b.conflict_status = 'usable'
         )"""
    market_conflict_binding = """AND EXISTS (
             SELECT 1 FROM phase2_market_observation_bindings b
             WHERE b.observation_id = m.observation_id
               AND b.request_id = ? AND b.scope_id = ?
               AND b.conflict_status = 'conflict'
         )"""
    evidence_binding = """AND EXISTS (
             SELECT 1 FROM phase2_evidence_bindings b
             WHERE b.evidence_id = e.evidence_id
               AND b.request_id = ? AND b.scope_id = ?
               AND b.conflict_status = 'usable'
         )"""
    evidence_conflict_binding = """AND EXISTS (
             SELECT 1 FROM phase2_evidence_bindings b
             WHERE b.evidence_id = e.evidence_id
               AND b.request_id = ? AND b.scope_id = ?
               AND b.conflict_status = 'conflict'
         )"""
    market_binding_parameters = (bound_request_id, selected_scope_id)
    evidence_binding_parameters = (bound_request_id, selected_scope_id)
    universe = connection.execute(
        """SELECT u.* FROM phase2_universe_members u
           JOIN phase2_refresh_scope_members sm ON sm.member_id = u.member_id
           WHERE sm.scope_id = ? ORDER BY u.ticker, u.effective_date, u.member_id""", (selected_scope_id,),
    ).fetchall()
    market_rows = connection.execute(
        """SELECT m.* FROM phase2_market_observations m
           WHERE m.observation_date <= ? AND substr(m.retrieved_at, 1, 10) <= ?
             AND freshness_status IN ('current', 'observed')
             AND EXISTS (
                 SELECT 1 FROM phase2_refresh_scope_members sm
                 JOIN phase2_universe_members u ON u.member_id = sm.member_id
                 WHERE sm.scope_id = ? AND u.ticker = m.ticker
             )
             {market_binding}
           ORDER BY m.ticker, m.observation_date, m.field, m.observation_id""".format(
               market_binding=market_binding,
           ),
        (cutoff, cutoff, selected_scope_id, *market_binding_parameters),
    ).fetchall()
    market = [dict(row, conflict_status="usable") for row in market_rows]
    market_conflict_rows = connection.execute(
        """SELECT m.* FROM phase2_market_observations m
           WHERE m.observation_date <= ? AND substr(m.retrieved_at, 1, 10) <= ?
             AND EXISTS (
                 SELECT 1 FROM phase2_refresh_scope_members sm
                 JOIN phase2_universe_members u ON u.member_id = sm.member_id
                 WHERE sm.scope_id = ? AND u.ticker = m.ticker
             )
             {market_conflict_binding}
           ORDER BY m.ticker, m.observation_date, m.field, m.observation_id""".format(
               market_conflict_binding=market_conflict_binding,
           ),
        (cutoff, cutoff, selected_scope_id, *market_binding_parameters),
    ).fetchall()
    market_conflicts = [dict(row, conflict_status="conflict") for row in market_conflict_rows]
    evidence_rows = connection.execute(
        """SELECT e.* FROM phase2_evidence e
           WHERE (e.publication_at IS NULL OR substr(e.publication_at, 1, 10) <= ?)
             AND substr(retrieved_at, 1, 10) <= ?
             AND recency_status IN ('recent', 'old')
             AND EXISTS (
                 SELECT 1 FROM phase2_refresh_scope_members sm
                 JOIN phase2_universe_members u ON u.member_id = sm.member_id
                 WHERE sm.scope_id = ? AND u.ticker = e.ticker
                   AND (e.issuer_cik IS NULL OR e.issuer_cik = u.cik)
             )
             {evidence_binding}
           ORDER BY e.ticker, e.publication_at, e.evidence_id""".format(
               evidence_binding=evidence_binding,
           ),
        (cutoff, cutoff, selected_scope_id, *evidence_binding_parameters),
    ).fetchall()
    evidence = [dict(row, conflict_status="usable") for row in evidence_rows]
    conflict_rows = connection.execute(
        """SELECT e.* FROM phase2_evidence e
           WHERE (e.publication_at IS NULL OR substr(e.publication_at, 1, 10) <= ?)
             AND substr(retrieved_at, 1, 10) <= ?
             AND EXISTS (
                 SELECT 1 FROM phase2_refresh_scope_members sm
                 JOIN phase2_universe_members u ON u.member_id = sm.member_id
                 WHERE sm.scope_id = ? AND u.ticker = e.ticker
                   AND (e.issuer_cik IS NULL OR e.issuer_cik = u.cik)
             )
             {evidence_conflict_binding}
           ORDER BY e.ticker, e.publication_at, e.evidence_id""".format(
               evidence_conflict_binding=evidence_conflict_binding,
           ),
        (cutoff, cutoff, selected_scope_id, *evidence_binding_parameters),
    ).fetchall()
    conflicts = [dict(row, conflict_status="conflict") for row in conflict_rows]
    failures = connection.execute(
        """SELECT failure_id, request_id, scope_id, source_name, failure_code, message, ticker, observed_at
           FROM phase2_failures
           WHERE request_id = ? AND substr(COALESCE(observed_at, created_at), 1, 10) <= ?
             AND scope_id = ?
           ORDER BY failure_id""", (bound_request_id, cutoff, selected_scope_id),
    ).fetchall()
    body: dict[str, object] = {
        "phase": "phase2-evidence", "as_of": cutoff, "request_id": bound_request_id, "scope_id": selected_scope_id,
        "no_verdict_boundary": "evidence_only",
        "universe": _json_safe_rows(universe), "market_observations": _json_safe_rows(market),
        "market_conflicts": _json_safe_rows(market_conflicts), "evidence": _json_safe_rows(evidence),
        "conflicts": _json_safe_rows(conflicts), "failures": _json_safe_rows(failures),
        "counts": {"universe": len(universe), "market_observations": len(market), "market_conflicts": len(market_conflicts), "evidence": len(evidence), "conflicts": len(conflicts), "failures": len(failures)},
    }
    _assert_evidence_only(body)
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    snapshot_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    snapshot_id = f"P2S-{snapshot_hash[:24]}"
    report = {**body, "snapshot_id": snapshot_id, "snapshot_hash": snapshot_hash}
    connection.execute(
        """INSERT OR IGNORE INTO phase2_snapshots(
            snapshot_id, as_of, created_at, snapshot_hash, report_json, no_verdict_boundary,
            request_id, scope_id
        ) VALUES (?, ?, ?, ?, ?, 'evidence_only', ?, ?)""",
        (snapshot_id, cutoff, datetime.now(timezone.utc).replace(microsecond=0).isoformat(), snapshot_hash, json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False), bound_request_id, selected_scope_id),
    )
    if commit:
        connection.commit()
    return report


_FINGERPRINT_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|token|secret|password|credential|authorization)",
    re.IGNORECASE,
)


def _safe_fingerprint_value(value: object, *, key: str = "") -> object:
    if _FINGERPRINT_SENSITIVE_KEY.search(key):
        if value is None:
            return None
        if isinstance(value, bytes):
            raw = value
        elif isinstance(value, str):
            raw = value.encode("utf-8")
        else:
            return {"configured": bool(value)}
        return {"sha256": hashlib.sha256(raw).hexdigest(), "length": len(raw)}
    if isinstance(value, Mapping):
        return {
            str(child_key): _safe_fingerprint_value(child_value, key=str(child_key))
            for child_key, child_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_fingerprint_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _file_content_hash(path: Path, label: str) -> dict[str, object]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} input could not be read") from exc
    return {"name": path.name, "sha256": hashlib.sha256(content).hexdigest(), "length": len(content)}


def _input_fingerprint(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        _safe_fingerprint_value(payload), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fixture_refresh_fingerprint(
    *, universe_path: Path, market_path: Path, as_of: str,
    sec_path: Path | None, news_path: Path | None,
    sec_ticker: str | None, sec_cik: str | None,
) -> str:
    return _input_fingerprint({
        "kind": "fixtures", "as_of": as_of,
        "universe": _file_content_hash(universe_path, "universe"),
        "market": _file_content_hash(market_path, "market"),
        "sec": None if sec_path is None else _file_content_hash(sec_path, "SEC"),
        "news": None if news_path is None else _file_content_hash(news_path, "RSS"),
        "sec_ticker": sec_ticker.upper() if isinstance(sec_ticker, str) else None,
        "sec_cik": sec_cik,
    })


def _configured_refresh_fingerprint(
    *, config_path: Path, universe_path: Path, as_of: str,
) -> str:
    try:
        config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Phase 2 config input could not be read") from exc
    if not isinstance(config_payload, dict):
        raise ValueError("Phase 2 config must be a JSON object")
    environment_hashes: dict[str, object] = {}
    for provider_config in (_config_value(config_payload, "market"),):
        environment_name = provider_config.get("api_key_env")
        if isinstance(environment_name, str) and environment_name:
            environment_value = os.environ.get(environment_name)
            environment_hashes[environment_name] = _safe_fingerprint_value(
                environment_value, key="api_key",
            )
    return _input_fingerprint({
        "kind": "configured", "as_of": as_of,
        "universe": _file_content_hash(universe_path, "universe"),
        "config": config_payload,
        "environment_secrets": environment_hashes,
    })


def _existing_run(
    connection: sqlite3.Connection, request_id: str, as_of: str,
    input_fingerprint: str,
) -> Phase2RefreshResult | None:
    _validate_request_id(request_id)
    row = connection.execute("SELECT * FROM phase2_refresh_runs WHERE request_id = ?", (request_id,)).fetchone()
    if row is None:
        return None
    if row["as_of"] != as_of:
        raise ValueError("request_id is already bound to a different as_of cutoff")
    stored_fingerprint = str(row["input_fingerprint"] or "") if "input_fingerprint" in row.keys() else ""
    if stored_fingerprint and stored_fingerprint != input_fingerprint:
        raise ValueError("request_id replay input fingerprint mismatch")
    if not stored_fingerprint:
        raise ValueError(
            "request_id replay is unverifiable because its persisted input fingerprint is blank; use a new request_id"
        )
    return Phase2RefreshResult(
        run_id=row["run_id"], accepted_universe_members=row["accepted_universe_members"],
        accepted_market_observations=row["accepted_market_observations"], accepted_evidence=row["accepted_evidence"],
        failure_count=row["failure_count"], snapshot_id=row["snapshot_id"], status=row["status"],
    )


def _finish_run(
    connection: sqlite3.Connection, *, request_id: str, as_of: str, started_at: str,
    accepted_universe: int, accepted_market: int, accepted_evidence: int, status: str,
    scope_id: str | None = None, input_fingerprint: str,
) -> Phase2RefreshResult:
    _validate_request_id(request_id)
    if scope_id is None:
        raise ValueError("Phase 2 refresh completion requires an explicit request scope")
    _validate_scope_request(connection, request_id=request_id, scope_id=scope_id)
    report = phase2_evidence_report(connection, as_of=as_of, commit=False, scope_id=scope_id, request_id=request_id)
    failure_count = connection.execute(
        "SELECT COUNT(*) FROM phase2_failures WHERE request_id = ? AND scope_id = ?",
        (request_id, scope_id),
    ).fetchone()[0]
    run_id = _canonical_id("P2R", [request_id, as_of])
    completed = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    connection.execute(
        """INSERT OR IGNORE INTO phase2_refresh_runs(
            run_id, request_id, as_of, started_at, completed_at, status,
            accepted_universe_members, accepted_market_observations, accepted_evidence,
            failure_count, snapshot_id, input_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, request_id, as_of, started_at, completed, status, accepted_universe,
         accepted_market, accepted_evidence, failure_count, report["snapshot_id"], input_fingerprint),
    )
    connection.commit()
    return Phase2RefreshResult(run_id, accepted_universe, accepted_market, accepted_evidence, failure_count, str(report["snapshot_id"]), status)


def _load_sec_fixture(path: Path, *, ticker: str | None, cik: str | None) -> tuple[str, str, Mapping[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("target"), dict) and isinstance(payload.get("payload"), dict):
        target = payload["target"]
        return str(target.get("ticker", "")), str(target.get("cik", "")), payload["payload"]
    if ticker is None or cik is None:
        raise ValueError("SEC fixture requires an explicit ticker and CIK target")
    if not isinstance(payload, dict):
        raise ValueError("SEC fixture payload must be a JSON object")
    return ticker, cik, payload


def refresh_phase2_fixtures(
    connection: sqlite3.Connection, *, universe_path: Path, market_path: Path,
    as_of: str, request_id: str, sec_path: Path | None = None, news_path: Path | None = None,
    sec_ticker: str | None = None, sec_cik: str | None = None,
) -> Phase2RefreshResult:
    cutoff = _iso_date(as_of, "as_of")
    _validate_request_id(request_id)
    input_fingerprint = _fixture_refresh_fingerprint(
        universe_path=universe_path, market_path=market_path, as_of=cutoff,
        sec_path=sec_path, news_path=news_path, sec_ticker=sec_ticker, sec_cik=sec_cik,
    )
    existing = _existing_run(connection, request_id, cutoff, input_fingerprint)
    if existing is not None:
        return existing
    started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    event_at = f"{cutoff}T00:00:00+00:00"
    accepted_universe = accepted_market = accepted_evidence = 0
    scope_id: str | None = None
    attempted_scope_id = _request_scope_id(connection, request_id=request_id)
    try:
        connection.execute("BEGIN")
        accepted_universe = import_sp500_universe(connection, universe_path, request_id=request_id, commit=False)
        members = _selected_current_members(connection, universe_path, as_of=cutoff)
        if attempted_scope_id is not None:
            _persist_refresh_scope(
                connection, request_id=request_id, as_of=cutoff,
                member_ids=[row["member_id"] for row in members],
            )
        if not members:
            raise ValueError("Phase 2 refresh universe has no active members at the requested as_of")
        scope_id = _persist_refresh_scope(
            connection, request_id=request_id, as_of=cutoff,
            member_ids=[row["member_id"] for row in members],
        )
        tickers = [row["ticker"] for row in members]
        accepted_market = refresh_market_observations(connection, FixtureMarketProvider(market_path), tickers, request_id=request_id, retrieved_at=event_at, commit=False, as_of=cutoff, scope_id=scope_id)
        if sec_path is not None:
            target_ticker, target_cik, payload = _load_sec_fixture(sec_path, ticker=sec_ticker, cik=sec_cik)
            normalized_target_ticker = target_ticker.upper()
            normalized_target_cik = str(target_cik).zfill(10)
            if not any(row["ticker"] == normalized_target_ticker and row["cik"] == normalized_target_cik for row in members):
                raise ValueError("SEC fixture target is not a current active universe member at the analysis cutoff")
            issues: list[str] = []
            records = normalize_sec_submissions(payload, ticker=target_ticker, cik=target_cik, retrieved_at=event_at, issues=issues)
            for issue in issues:
                _record_failure(connection, request_id=request_id, source_name="SEC EDGAR", failure_code="unsupported_or_malformed_evidence", message=issue, ticker=target_ticker, observed_at=event_at, scope_id=scope_id)
            accepted_evidence += ingest_evidence(
                connection, records, request_id=request_id, commit=False, as_of=cutoff, scope_id=scope_id,
            )
        if news_path is not None:
            aliases = {row["ticker"]: (row["ticker"], row["issuer_name"]) for row in members}
            config = RssSourceConfig(source_name="configured-rss-fixture", source_url="https://example.test/feed.xml", source_version="phase2-rss-fixture-v1", terms_url="https://example.test/terms", entity_aliases=aliases, content_policy="metadata_only")
            accepted_evidence += ingest_evidence(
                connection, normalize_rss_feed(news_path.read_bytes(), config=config, retrieved_at=event_at),
                request_id=request_id, commit=False, as_of=cutoff, scope_id=scope_id,
            )
        return _finish_run(connection, request_id=request_id, as_of=cutoff, started_at=started, accepted_universe=accepted_universe, accepted_market=accepted_market, accepted_evidence=accepted_evidence, status="completed_with_warnings" if connection.execute("SELECT 1 FROM phase2_failures WHERE request_id = ? AND scope_id = ? LIMIT 1", (request_id, scope_id)).fetchone() else "completed", scope_id=scope_id, input_fingerprint=input_fingerprint)
    except _RequestScopeConflict:
        connection.rollback()
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        connection.rollback()
        scope_id = attempted_scope_id
        if scope_id is None:
            scope_id = _persist_refresh_scope(
                connection, request_id=request_id, as_of=cutoff, member_ids=[],
            )
        else:
            _validate_scope_request(connection, request_id=request_id, scope_id=scope_id)
        connection.commit()
        _record_failure(connection, request_id=request_id, source_name="phase2-refresh", failure_code="refresh_failed", message=str(exc), observed_at=event_at, scope_id=attempted_scope_id)
        return _finish_run(connection, request_id=request_id, as_of=cutoff, started_at=started, accepted_universe=0, accepted_market=0, accepted_evidence=0, status="failed", scope_id=scope_id, input_fingerprint=input_fingerprint)


def _config_value(config: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = config.get(name)
    return value if isinstance(value, dict) else {}


def _configured_timeout(value: object, default: float) -> float:
    if isinstance(value, bool):
        raise ValueError("provider timeout must be numeric and must not be boolean")
    try:
        timeout = float(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider timeout must be numeric") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 30:
        raise ValueError("provider timeout must be greater than zero and at most 30 seconds")
    return timeout


def _configured_sec_rate(value: object, default: float = 10) -> float:
    if isinstance(value, bool):
        raise ValueError("SEC max_requests_per_second must be numeric and must not be boolean")
    try:
        rate = float(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ValueError("SEC max_requests_per_second must be numeric") from exc
    if not math.isfinite(rate) or not 0 < rate <= 10:
        raise ValueError("SEC max_requests_per_second must be greater than zero and at most 10")
    return rate


def refresh_phase2_configured(
    connection: sqlite3.Connection, *, config_path: Path, universe_path: Path, as_of: str,
    request_id: str, transport: Any | None = None,
    clock: Callable[[], str] | None = None,
    monotonic_clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> Phase2RefreshResult:
    """Run the configured read-only Alpha Vantage, SEC, and RSS adapters."""
    cutoff = _iso_date(as_of, "as_of")
    _validate_request_id(request_id)
    input_fingerprint = _configured_refresh_fingerprint(
        config_path=config_path, universe_path=universe_path, as_of=cutoff,
    )
    existing = _existing_run(connection, request_id, cutoff, input_fingerprint)
    if existing is not None:
        return existing
    event_at = normalize_retrieved_at((clock or utc_now)())
    started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    members: list[sqlite3.Row] = []
    accepted_universe = accepted_market = accepted_evidence = 0
    scope_id: str | None = None
    attempted_scope_id = _request_scope_id(connection, request_id=request_id)
    try:
        config_payload = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config_payload, dict):
            raise ValueError("Phase 2 config must be a JSON object")
        connection.execute("BEGIN")
        accepted_universe = import_sp500_universe(connection, universe_path, request_id=request_id, commit=False)
        members = _selected_current_members(connection, universe_path, as_of=cutoff)
        if attempted_scope_id is not None:
            _persist_refresh_scope(
                connection, request_id=request_id, as_of=cutoff,
                member_ids=[row["member_id"] for row in members],
            )
        if not members:
            raise ValueError("Phase 2 refresh universe has no active members at the requested as_of")
        scope_id = _persist_refresh_scope(
            connection, request_id=request_id, as_of=cutoff,
            member_ids=[row["member_id"] for row in members],
        )
        market_cfg = _config_value(config_payload, "market")
        market_base = market_cfg.get("base_url", "https://www.alphavantage.co/query")
        sec_cfg = _config_value(config_payload, "sec")
        rss_cfg = _config_value(config_payload, "rss")
        rss_url = rss_cfg.get("source_url")
        rss_host = ""
        if isinstance(rss_url, str) and not rss_url.startswith("[REDACTED"):
            try:
                _validate_public_reference(rss_url, "RSS source_url")
            except ValueError:
                pass
            else:
                rss_host = (urllib.parse.urlsplit(rss_url).hostname or "").lower().rstrip(".")
        runtime_transport = transport if transport is not None else UrllibReadOnlyTransport(
            frozenset(ALPHA_VANTAGE_HOSTS | {"data.sec.gov"} | ({rss_host} if rss_host else set()))
        )
        configured_provider_succeeded = False
        if market_cfg.get("enabled", True) is False:
            _record_failure(connection, request_id=request_id, source_name="alpha-vantage", failure_code="source_deferred", message="Alpha Vantage market provider is disabled in configuration", observed_at=event_at, scope_id=scope_id)
        elif market_cfg.get("provider") != "alpha-vantage":
            _record_failure(connection, request_id=request_id, source_name="alpha-vantage", failure_code="configuration_required", message="Alpha Vantage market provider is not configured", observed_at=event_at, scope_id=scope_id)
        else:
            try:
                if not isinstance(market_base, str) or market_base.startswith("[REDACTED"):
                    raise ValueError("Alpha Vantage base_url is not configured")
                validate_provider_url(market_base, "Alpha Vantage base_url", ALPHA_VANTAGE_HOSTS)
            except ValueError as exc:
                _record_failure(connection, request_id=request_id, source_name="alpha-vantage", failure_code="configuration_required", message=str(exc), observed_at=event_at, scope_id=scope_id)
            else:
                key = market_cfg.get("api_key") if isinstance(market_cfg.get("api_key"), str) else None
                env_name = market_cfg.get("api_key_env")
                if key is None and isinstance(env_name, str) and not env_name.startswith("[REDACTED"):
                    key = os.environ.get(env_name)
                if not key or str(key).startswith("[REDACTED"):
                    _record_failure(connection, request_id=request_id, source_name="alpha-vantage", failure_code="configuration_required", message="Alpha Vantage API key is not configured", observed_at=event_at, scope_id=scope_id)
                else:
                    from .evidence_providers import AlphaVantageMarketProvider
                    market_usable: list[bool] = []
                    accepted_market = refresh_market_observations(connection, AlphaVantageMarketProvider(str(key), runtime_transport, base_url=market_base, timeout_seconds=_configured_timeout(market_cfg.get("timeout_seconds"), 15)), [row["ticker"] for row in members], request_id=request_id, retrieved_at=event_at, commit=False, sensitive_values=(str(key),), usable_result=market_usable, as_of=cutoff, scope_id=scope_id)
                    configured_provider_succeeded = bool(market_usable and market_usable[0])
        user_agent = sec_cfg.get("user_agent")
        if sec_cfg.get("enabled", True) is False:
            _record_failure(connection, request_id=request_id, source_name="SEC EDGAR", failure_code="source_deferred", message="SEC provider is disabled in configuration", observed_at=event_at, scope_id=scope_id)
        elif not isinstance(user_agent, str) or not user_agent.strip() or user_agent.startswith("[REDACTED"):
            _record_failure(connection, request_id=request_id, source_name="SEC EDGAR", failure_code="configuration_required", message="SEC descriptive User-Agent is not configured", observed_at=event_at, scope_id=scope_id)
        else:
            try:
                limiter = RequestRateLimiter(
                    _configured_sec_rate(sec_cfg.get("max_requests_per_second")),
                    clock=monotonic_clock or time.monotonic,
                    sleeper=sleeper or time.sleep,
                )
                sec_provider = SECEdgarProvider(runtime_transport, user_agent, submissions_base_url=str(sec_cfg.get("submissions_base_url", "https://data.sec.gov/submissions/")), companyfacts_base_url=str(sec_cfg.get("companyfacts_base_url", "https://data.sec.gov/api/xbrl/companyfacts/")), timeout_seconds=_configured_timeout(sec_cfg.get("timeout_seconds"), 20), limiter=limiter)
            except (TypeError, ValueError) as exc:
                _record_failure(connection, request_id=request_id, source_name="SEC EDGAR", failure_code="configuration_required", message=str(exc), observed_at=event_at, scope_id=scope_id)
            else:
                for row in members:
                    for operation in ("submissions", "companyfacts"):
                        try:
                            issues: list[str] = []
                            records = sec_provider.fetch_submissions(row["cik"], ticker=row["ticker"], retrieved_at=event_at, issues=issues) if operation == "submissions" else sec_provider.fetch_company_facts(row["cik"], ticker=row["ticker"], retrieved_at=event_at, issues=issues)
                            for issue in issues:
                                _record_failure(connection, request_id=request_id, source_name="SEC EDGAR", failure_code="unsupported_or_malformed_evidence", message=issue, ticker=row["ticker"], observed_at=event_at, scope_id=scope_id)
                            evidence_usable: list[bool] = []
                            accepted_from_operation = ingest_evidence(connection, records, request_id=request_id, commit=False, as_of=cutoff, usable_result=evidence_usable, scope_id=scope_id)
                            accepted_evidence += accepted_from_operation
                            configured_provider_succeeded = configured_provider_succeeded or bool(evidence_usable and evidence_usable[0])
                            if not evidence_usable or not evidence_usable[0]:
                                _record_failure(
                                    connection, request_id=request_id, source_name="SEC EDGAR",
                                    failure_code="no_usable_evidence",
                                    message=f"SEC {operation} response contained no usable evidence",
                                    ticker=row["ticker"], observed_at=event_at, scope_id=scope_id,
                                )
                        except (ProviderUnavailable, OSError, RuntimeError, TypeError, ValueError) as exc:
                            _record_failure(connection, request_id=request_id, source_name="SEC EDGAR", failure_code="provider_unavailable" if isinstance(exc, ProviderUnavailable) else "invalid_provider_response", message=str(exc), ticker=row["ticker"], observed_at=event_at, scope_id=scope_id)
        if rss_cfg.get("enabled", True) is not False:
            aliases = rss_cfg.get("entity_aliases")
            if not isinstance(rss_url, str) or rss_url.startswith("[REDACTED") or not isinstance(aliases, dict) or not aliases:
                _record_failure(connection, request_id=request_id, source_name="RSS", failure_code="configuration_required", message="RSS URL and explicit entity aliases are not configured", observed_at=event_at, scope_id=scope_id)
            else:
                eligible_tickers = {row["ticker"] for row in members}
                eligible_aliases = {
                    str(k): tuple(str(alias) for alias in value)
                    for k, value in aliases.items()
                    if str(k).upper() in eligible_tickers and isinstance(value, list)
                }
                rss_config = RssSourceConfig(source_name=str(rss_cfg.get("source_name", "configured-rss")), source_url=rss_url, source_version=str(rss_cfg.get("source_version", "configured-rss-v1")), terms_url=str(rss_cfg.get("terms_url", "")), entity_aliases=eligible_aliases, content_policy=str(rss_cfg.get("content_policy", "metadata_only")))
                try:
                    evidence_usable: list[bool] = []
                    accepted_evidence += ingest_evidence(connection, RssFeedProvider(runtime_transport, rss_config, timeout_seconds=_configured_timeout(rss_cfg.get("timeout_seconds"), 15)).fetch(retrieved_at=event_at), request_id=request_id, commit=False, as_of=cutoff, usable_result=evidence_usable, scope_id=scope_id)
                    configured_provider_succeeded = configured_provider_succeeded or bool(evidence_usable and evidence_usable[0])
                    if not evidence_usable or not evidence_usable[0]:
                        _record_failure(connection, request_id=request_id, source_name="RSS", failure_code="no_usable_evidence", message="RSS response contained no usable evidence", observed_at=event_at, scope_id=scope_id)
                except (ProviderUnavailable, OSError, TypeError, ValueError) as exc:
                    _record_failure(connection, request_id=request_id, source_name="RSS", failure_code="provider_unavailable" if isinstance(exc, ProviderUnavailable) else "invalid_provider_response", message=str(exc), observed_at=event_at, scope_id=scope_id)
        else:
            _record_failure(connection, request_id=request_id, source_name="RSS", failure_code="source_deferred", message="RSS provider is disabled in configuration", observed_at=event_at, scope_id=scope_id)
        status = "completed_with_warnings" if configured_provider_succeeded else "failed"
        return _finish_run(connection, request_id=request_id, as_of=cutoff, started_at=started, accepted_universe=accepted_universe, accepted_market=accepted_market, accepted_evidence=accepted_evidence, status=status, scope_id=scope_id, input_fingerprint=input_fingerprint)
    except _RequestScopeConflict:
        connection.rollback()
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        connection.rollback()
        scope_id = attempted_scope_id
        if scope_id is None:
            scope_id = _persist_refresh_scope(
                connection, request_id=request_id, as_of=cutoff, member_ids=[],
            )
        else:
            _validate_scope_request(connection, request_id=request_id, scope_id=scope_id)
        connection.commit()
        _record_failure(connection, request_id=request_id, source_name="phase2-configured-refresh", failure_code="refresh_failed", message=str(exc), observed_at=event_at, scope_id=attempted_scope_id)
        return _finish_run(connection, request_id=request_id, as_of=cutoff, started_at=started, accepted_universe=0, accepted_market=0, accepted_evidence=0, status="failed", scope_id=scope_id, input_fingerprint=input_fingerprint)
