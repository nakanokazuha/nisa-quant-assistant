"""Responsibility-level Phase 3 producer for explicit live/replay refreshes."""
from __future__ import annotations

import json
import hashlib
import re
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping

from .evidence_providers import HttpResponse, ProviderUnavailable, UrllibReadOnlyTransport
from .historical_market_data import (
    HistorySnapshot,
    SecFact,
    UniverseMember,
    _content_hash,
    _validate_request_contract,
    build_sec_request_contract,
    fetch_sec_company_facts_for_ticker,
    fetch_history_snapshot,
    has_point_in_time_membership_evidence,
    load_history_snapshot,
    parse_retrieved_at,
    SEC_CACHE_MAX_AGE_DAYS,
)
from .phase3_reporting import render_phase3_report
from .ranking_model import SpecializedRankingModel, current_prediction_freshness, rank_current_candidates
from .report_rendering import validate_report_safety
from .training_dataset import build_monthly_panel
from .walk_forward_evaluation import walk_forward_backtest

SP500_UNIVERSE_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SP500_UNIVERSE_PROVIDER = "Wikipedia current S&P 500 constituent table; provider/licensing and point-in-time limits disclosed"
PHASE3_MANIFEST_SCHEMA = "phase3-refresh-manifest"
PHASE3_MANIFEST_VERSION = 1
MIN_PHASE3_MARKET_ROWS = 253
MIN_LIVE_COVERAGE_RATIO = 0.8
MIN_FULL_SP500_MEMBERS = 450
REFRESH_SUCCESS_STATUSES = frozenset({"available", "available_descriptive"})
REFRESH_REPORT_STATUSES = REFRESH_SUCCESS_STATUSES | {"unavailable_insufficient_data", "unavailable"}


def _strip_unusable_sec_facts(snapshot: HistorySnapshot, *, contract: Mapping[str, Any]) -> HistorySnapshot:
    retrieval_status = str(contract["retrieval_status"])
    retrieval_intent = str(contract["retrieval_intent"])
    if retrieval_status in {"bound", "legacy_fixture"}:
        retrieval_status = "bound_no_usable_facts"
        retrieval_intent = "explicit_company_facts_probe"
    sanitized_contract = build_sec_request_contract(
        ticker=contract.get("ticker"), cik=contract.get("cik"),
        provider=str(contract["provider"]), source_version=str(contract["source_version"]),
        requested_start=contract.get("requested_start"), requested_end=contract.get("requested_end"),
        as_of=contract.get("as_of"), retrieved_at=str(contract["retrieved_at"]),
        retrieval_intent=retrieval_intent, retrieval_status=retrieval_status, facts=(),
    )
    rebound = replace(snapshot, sec_facts=[], sec_request_contract=sanitized_contract, snapshot_id="")
    return replace(rebound, snapshot_id=f"phase3-{_content_hash(rebound)[:20]}")


def _provider_ticker(source_symbol: str) -> str:
    return source_symbol.strip().upper().replace(".", "-")


class _ConstituentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.table_depth = 0
        self.in_row = False
        self.in_cell = False
        self.cell_tag = ""
        self.cells: list[str] = []
        self.cell_text: list[str] = []
        self.rows: list[tuple[str, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        if tag == "table" and not self.in_table:
            class_name = attrs_map.get("class", "") or ""
            table_id = attrs_map.get("id", "") or ""
            if "wikitable" in class_name or "constituents" in table_id.lower():
                self.in_table = True
                self.table_depth = 1
                return
        elif tag == "table" and self.in_table:
            self.table_depth += 1
        if not self.in_table:
            return
        if tag == "tr":
            self.in_row = True
            self.cells = []
            self.cell_text = []
        elif tag in {"th", "td"} and self.in_row and not self.in_cell:
            self.in_cell = True
            self.cell_tag = tag
            self.cell_text = []

    def handle_endtag(self, tag: str) -> None:
        if not self.in_table:
            return
        if tag in {"th", "td"} and self.in_cell:
            self.cells.append(" ".join("".join(self.cell_text).split()))
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            self.rows.append(("row", self.cells.copy()))
            self.in_row = False
        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_table = False

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell_text.append(data)


def parse_current_sp500_html(
    html_bytes: bytes, *, retrieved_at: str, minimum_members: int = MIN_FULL_SP500_MEMBERS,
) -> list[UniverseMember]:
    if not isinstance(html_bytes, (bytes, bytearray)):
        raise ValueError("S&P 500 source must be bytes")
    parser = _ConstituentParser()
    try:
        parser.feed(bytes(html_bytes).decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("S&P 500 source is not valid UTF-8 HTML") from exc
    header: list[str] | None = None
    records: list[tuple[str, str]] = []
    for kind, cells in parser.rows:
        normalized = [re.sub(r"\[[^\]]+\]", "", cell).strip().casefold() for cell in cells]
        if header is None and "symbol" in normalized:
            header = normalized
            continue
        if header is None or "symbol" not in header:
            continue
        symbol_index = header.index("symbol")
        if len(cells) <= symbol_index:
            continue
        source_symbol = re.sub(r"\[[^\]]+\]", "", cells[symbol_index]).strip().upper()
        if source_symbol and source_symbol.casefold() != "symbol":
            records.append((source_symbol, cells[header.index("security")] if "security" in header and len(cells) > header.index("security") else ""))
    if not records:
        raise ValueError("S&P 500 source contains no constituent symbol column")
    deduped: dict[str, tuple[str, str]] = {}
    for source_symbol, security in sorted(records, key=lambda item: (_provider_ticker(item[0]), item[0], item[1])):
        deduped.setdefault(_provider_ticker(source_symbol), (source_symbol, security))
    if len(deduped) < minimum_members:
        raise ValueError(f"S&P 500 source contains only {len(deduped)} members; full-universe threshold is {minimum_members}")
    return [
        UniverseMember(
            ticker=ticker, effective_from=None, effective_to=None,
            membership_status="active", lookahead_bias_status="current_snapshot_only",
            survivorship_bias_status="survivorship_risk_disclosed", source=SP500_UNIVERSE_URL,
            source_version=retrieved_at, source_symbol=source_symbol,
        )
        for ticker, (source_symbol, _security) in sorted(deduped.items())
    ]


def fetch_current_sp500_universe(*, retrieved_at: str | None = None, http_get: Callable[..., HttpResponse] | None = None) -> list[UniverseMember]:
    stamp = retrieved_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if http_get is None:
        response = UrllibReadOnlyTransport(frozenset({"en.wikipedia.org"}), max_bytes=2_000_000).get(
            SP500_UNIVERSE_URL, headers={"User-Agent": "nisa-quant-assistant/phase3-read-only"}, timeout=20,
        )
    else:
        response = http_get(SP500_UNIVERSE_URL, headers={"User-Agent": "nisa-quant-assistant/phase3-read-only"}, timeout=20)
    if response.status_code != 200:
        raise ProviderUnavailable(f"S&P 500 source returned HTTP {response.status_code}")
    return parse_current_sp500_html(response.body, retrieved_at=stamp)


def _write_atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _manifest_gaps(snapshot: HistorySnapshot, failures_by_ticker: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    coverage = getattr(snapshot, "coverage", {})
    bars_by_ticker = getattr(snapshot, "bars_by_ticker", {})
    universe_tickers = {
        member.ticker for member in getattr(snapshot, "universe", ())
        if isinstance(getattr(member, "ticker", None), str)
    }
    tickers = sorted(
        {
            *coverage,
            *bars_by_ticker,
            *universe_tickers,
            *failures_by_ticker,
            getattr(snapshot, "benchmark_ticker", "^GSPC"),
        }
    )
    entries: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        details = coverage.get(ticker, {})
        if not isinstance(details, Mapping):
            details = {}
        observed_gaps = details.get("gaps", [])
        if not isinstance(observed_gaps, list):
            observed_gaps = []
        row_count = details.get("row_count", len(bars_by_ticker.get(ticker, ())))
        if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
            row_count = len(bars_by_ticker.get(ticker, ()))
        status = (
            "failed" if ticker in failures_by_ticker else
            "missing" if row_count == 0 else
            "gapped" if observed_gaps else
            "covered"
        )
        gap_count = details.get("gap_count", len(observed_gaps))
        entries[ticker] = {
            "status": status,
            "actual_first": details.get("actual_first"),
            "actual_last": details.get("actual_last"),
            "row_count": row_count,
            "gap_count": gap_count,
            "gaps": observed_gaps,
        }
    return entries


def _find_compatible_history_cache_paths(
    cache_dir: Path, *, start: str, end: str, benchmark_ticker: str = "^GSPC",
    expected_request_contract: str | None = None,
) -> list[Path]:
    """Find valid content-addressed history caches matching the replay request."""
    expected = None
    if expected_request_contract is not None:
        expected = _validate_request_contract(
            expected_request_contract, benchmark_ticker=benchmark_ticker,
        )
        if expected["start_date"] != start or expected["end_date"] != end:
            return []
    matches: list[Path] = []
    for path in sorted(cache_dir.glob("history-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            contract = payload.get("request_contract") if isinstance(payload, dict) else None
            if not isinstance(contract, str) or not contract:
                continue
            try:
                parsed = _validate_request_contract(contract, benchmark_ticker=benchmark_ticker)
            except ValueError:
                # Keep the historical producer test seam: a mocked loader may
                # supply a legacy two-field contract. Real loading still
                # validates the complete content-addressed artifact.
                legacy = json.loads(contract)
                if (
                    path.name != "history-cached.json"
                    or not isinstance(legacy, dict)
                    or set(legacy) != {"start_date", "end_date"}
                ):
                    continue
                if expected is not None or legacy["start_date"] != start or legacy["end_date"] != end:
                    continue
                matches.append(path)
                continue
            if expected is not None:
                if contract != expected_request_contract:
                    continue
            elif parsed["start_date"] != start or parsed["end_date"] != end:
                continue
            expected_filename = f"history-{hashlib.sha256(contract.encode()).hexdigest()[:24]}.json"
            if path.name != expected_filename:
                continue
            matches.append(path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return matches


def unavailable_refresh_report(*, reason: str, manifest: Mapping[str, Any], output_format: str) -> str | dict[str, Any]:
    payload = {
        "schema": "phase3-unavailable-report",
        "status": "unavailable",
        "performance_claims_unavailable": True,
        "reason": reason,
        "manifest": dict(manifest),
        "manual_only": True,
        "no_trading": True,
        "sec_interpretation": "SEC Company Facts are optional and may be partial; missing facts remain explicit and are not issuer-value substitutes",
    }
    if output_format == "json":
        validate_report_safety(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return payload
    report = "\n".join([
        "# NISA Quant Assistant Phase 3 Report", "", "## Status", "",
        "UNAVAILABLE — no performance claim is published.", "", f"Reason: {reason}", "",
        "The producer is read-only and cannot place orders.", "",
        "```json", json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False), "```", "",
    ])
    validate_report_safety(report)
    return report


def refresh_phase3(
    *, as_of: str, start: str, end: str, cache_dir: Path, output: Path,
    live: bool = False, replay_only: bool = True, limit: int | None = None,
    sec_contact: str | None = None, replay_request_contract: str | None = None,
) -> int:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    manifest: dict[str, Any] = {
        "schema": PHASE3_MANIFEST_SCHEMA, "schema_version": PHASE3_MANIFEST_VERSION,
        "mode": "live" if live else "replay-only", "as_of": as_of,
        "requested_start": start, "requested_end": end, "retrieved_at": stamp,
        "requested_limit": limit, "limit_applied": False, "universe_count_before_limit": 0,
        "universe_scope": "full_current_sp500" if live and limit is None else ("limited_live_smoke" if live else "replay_cache"),
        "universe_source_url": SP500_UNIVERSE_URL if live else None,
        "provider": "Yahoo Finance chart (unofficial/provider-risk)" if live else "local file cache",
        "request_contract": None, "benchmark_identity": "^GSPC", "source_urls": [],
        "first_observation": None, "last_observation": None, "row_counts": {},
        "gaps": {}, "failures": [], "artifact_ids": {}, "constituent_count": 0,
        "panel_excluded_by_ticker": {},
        "point_in_time_membership": False, "sec_status": "not_requested", "sec_rows": 0,
        "sec_status_by_ticker": {}, "failures_by_ticker": {},
        "sec_failure_evidence": [],
        "current_prediction_freshness": [],
        "coverage_policy": {
            "minimum_market_rows_per_ticker": MIN_PHASE3_MARKET_ROWS,
            "minimum_live_constituent_ratio": MIN_LIVE_COVERAGE_RATIO,
            "failed_or_insufficient_tickers_block_refresh_below_threshold": True,
        },
    }
    report_status = "unavailable"
    try:
        cutoff = date.fromisoformat(as_of)
        requested_start = date.fromisoformat(start)
        requested_end = date.fromisoformat(end)
        manifest["as_of"] = cutoff.isoformat()
        if requested_start > requested_end:
            raise ValueError("requested replay range is invalid")
        if live == replay_only:
            raise ValueError("exactly one of --live or --replay-only must be selected")
        if not live and limit is not None:
            raise ValueError("--limit is only valid with --live")
        if live and replay_request_contract is not None:
            raise ValueError("--request-contract is only valid with replay-only")
        if live and limit is not None and (isinstance(limit, bool) or limit <= 0):
            raise ValueError("live --limit must be a positive integer")
        sec_facts: list[SecFact] = []
        if live:
            universe = fetch_current_sp500_universe(retrieved_at=stamp)
            manifest["universe_count_before_limit"] = len(universe)
            if limit is not None:
                universe = universe[:limit]
                manifest["limit_applied"] = True
            tickers = [member.ticker for member in universe]
            manifest["source_urls"] = [SP500_UNIVERSE_URL, "https://query1.finance.yahoo.com/v8/finance/chart"]
            manifest["current_universe_retrieved_at"] = stamp
            manifest["sec_status_by_ticker"] = {ticker: "not_requested" for ticker in tickers}
            sec_status = "not_requested"
            sec_issues: list[str] = []
            sec_cik: str | None = None
            sec_ticker = tickers[0] if tickers else None
            if sec_contact and sec_ticker:
                try:
                    sec_facts = fetch_sec_company_facts_for_ticker(
                        sec_ticker, contact=sec_contact, retrieved_at=stamp, issues=sec_issues,
                    )
                    if sec_facts and re.fullmatch(r"\d{10}-\d{2}-\d{6}", sec_facts[0].accession):
                        sec_cik = sec_facts[0].accession[:10]
                    sec_status = "bound" if sec_facts else "bound_no_usable_facts"
                    manifest["sec_status"] = sec_status
                    manifest["sec_rows"] = len(sec_facts)
                    manifest["sec_failure_evidence"] = sec_issues
                    if sec_issues:
                        manifest["failures"].extend(f"SEC Company Facts: {issue}" for issue in sec_issues)
                    manifest["sec_status_by_ticker"].update({
                        ticker: "not_probed_bounded_probe" for ticker in tickers[1:]
                    })
                    manifest["sec_status_by_ticker"][sec_ticker] = {
                        "status": sec_status,
                        "row_count": len(sec_facts),
                    }
                except (OSError, ValueError, ProviderUnavailable) as exc:
                    sec_status = "failed" if isinstance(exc, ValueError) else "unavailable"
                    manifest["sec_status"] = sec_status
                    manifest["failures"].append(f"SEC Company Facts: {exc}")
                    manifest["sec_failure_evidence"] = [str(exc)]
                    manifest["sec_status_by_ticker"].update({
                        ticker: "not_probed_bounded_probe" for ticker in tickers[1:]
                    })
                    manifest["sec_status_by_ticker"][sec_ticker] = {
                        "status": sec_status, "reason": str(exc),
                    }
            else:
                manifest["sec_status"] = "not_requested"
            sec_request_contract = build_sec_request_contract(
                ticker=sec_ticker if sec_contact and sec_ticker else None, cik=sec_cik, provider="SEC XBRL Company Facts",
                source_version="companyfacts-v1", requested_start=requested_start,
                requested_end=requested_end, as_of=cutoff, retrieved_at=stamp,
                retrieval_intent="explicit_company_facts_probe" if sec_contact and sec_ticker else "sec_not_requested",
                retrieval_status=sec_status, facts=sec_facts,
            )
            snapshot = fetch_history_snapshot(
                tickers=tickers, benchmark_ticker="^GSPC", start_date=start, end_date=end,
                cache_dir=cache_dir, retrieved_at=stamp, universe=universe,
                sec_facts=sec_facts, allow_partial=True, sec_request_contract=sec_request_contract,
                allow_cache_reuse=False,
            )
            if isinstance(snapshot, HistorySnapshot):
                rebound = replace(snapshot, sec_facts=list(sec_facts), sec_request_contract=sec_request_contract, snapshot_id="")
                snapshot = replace(rebound, snapshot_id=f"phase3-{_content_hash(rebound)[:20]}")
        else:
            candidates = _find_compatible_history_cache_paths(
                cache_dir, start=requested_start.isoformat(), end=requested_end.isoformat(),
                benchmark_ticker="^GSPC", expected_request_contract=replay_request_contract,
            )
            if len(candidates) != 1:
                raise ValueError("replay-only requires exactly one compatible history cache artifact for the requested range")
            snapshot = load_history_snapshot(
                candidates[0], expected_request_contract=replay_request_contract,
            )
            cached_sec_facts = list(getattr(snapshot, "sec_facts", ()))
            cached_sec_contract = getattr(snapshot, "sec_request_contract", "")
            cached_status = None
            if cached_sec_contract:
                try:
                    cached_status = json.loads(cached_sec_contract).get("retrieval_status")
                except (TypeError, json.JSONDecodeError):
                    cached_status = None
            cached_contract: dict[str, Any] | None = None
            if cached_sec_contract:
                try:
                    parsed_cached_contract = json.loads(cached_sec_contract)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("cached SEC request contract is invalid") from exc
                if not isinstance(parsed_cached_contract, dict):
                    raise ValueError("cached SEC request contract is invalid")
                cached_contract = parsed_cached_contract
            cached_sec_stale = False
            if cached_sec_facts and cached_contract is not None:
                try:
                    cached_retrieved_at = parse_retrieved_at(cached_contract["retrieved_at"])
                    cache_age_days = (date.fromisoformat(stamp[:10]) - cached_retrieved_at.date()).days
                except (KeyError, TypeError, ValueError):
                    cached_sec_stale = True
                else:
                    cached_sec_stale = not 0 <= cache_age_days <= SEC_CACHE_MAX_AGE_DAYS
                if cached_sec_stale:
                    snapshot = _strip_unusable_sec_facts(snapshot, contract=cached_contract)
                    cached_sec_facts = []
            if cached_status in {"unavailable", "failed", "not_requested", "bound_no_usable_facts"} and cached_sec_facts:
                snapshot = _strip_unusable_sec_facts(snapshot, contract=cached_contract or {})
                cached_sec_facts = []
            if cached_sec_stale:
                cached_ticker = cached_contract.get("ticker") if cached_contract else None
                evidence = "cached SEC facts excluded: retrieval is stale"
                manifest["sec_status"] = "bound_no_usable_facts"
                manifest["sec_rows"] = 0
                manifest["sec_failure_evidence"] = [evidence]
                manifest["failures"].append(f"SEC Company Facts {evidence}")
                manifest["sec_status_by_ticker"] = {
                    ticker: {
                        "status": "bound_no_usable_facts" if ticker == cached_ticker else "not_probed_bounded_probe",
                        "row_count": 0,
                    }
                    for ticker in (member.ticker for member in snapshot.universe)
                }
            elif cached_sec_facts and cached_status in {"bound", "legacy_fixture", None}:
                fact_counts: dict[str, int] = {}
                for fact in cached_sec_facts:
                    ticker = getattr(fact, "ticker", None)
                    if isinstance(ticker, str) and ticker:
                        fact_counts[ticker] = fact_counts.get(ticker, 0) + 1
                manifest["sec_status"] = "cached_partial"
                manifest["sec_rows"] = len(cached_sec_facts)
                manifest["sec_status_by_ticker"] = {
                    ticker: {
                        "status": "cached" if fact_counts.get(ticker, 0) else "not_requested",
                        "row_count": fact_counts.get(ticker, 0),
                    }
                    for ticker in (member.ticker for member in snapshot.universe)
                }
            elif cached_status == "bound_no_usable_facts":
                cached_ticker = cached_contract.get("ticker") if cached_contract else None
                manifest["sec_status"] = "bound_no_usable_facts"
                manifest["sec_rows"] = 0
                manifest["sec_status_by_ticker"] = {
                    ticker: {"status": "bound_no_usable_facts", "row_count": 0}
                    if ticker == cached_ticker else "not_probed_bounded_probe"
                    for ticker in (member.ticker for member in snapshot.universe)
                }
            elif cached_status == "not_requested":
                manifest["sec_status"] = "not_requested"
                manifest["sec_rows"] = 0
                manifest["sec_status_by_ticker"] = {
                    ticker: "not_requested" for ticker in (member.ticker for member in snapshot.universe)
                }
            elif cached_status in {"unavailable", "failed"}:
                cached_ticker = cached_contract.get("ticker") if cached_contract else None
                evidence = f"cached SEC request status: {cached_status}"
                manifest["sec_status"] = cached_status
                manifest["sec_rows"] = 0
                manifest["sec_failure_evidence"] = [evidence]
                manifest["failures"].append(f"SEC Company Facts {evidence}")
                manifest["sec_status_by_ticker"] = {
                    ticker: {
                        "status": cached_status if ticker == cached_ticker else "not_probed_bounded_probe",
                        "row_count": 0,
                    }
                    for ticker in (member.ticker for member in snapshot.universe)
                }
            else:
                manifest["sec_status"] = "unavailable"
                manifest["sec_rows"] = 0
                manifest["sec_failure_evidence"] = ["cached SEC request status is missing"]
        manifest["request_contract"] = snapshot.request_contract
        manifest["artifact_ids"]["history_snapshot_id"] = snapshot.snapshot_id
        manifest["constituent_count"] = len(snapshot.universe)
        manifest["point_in_time_membership"] = bool(snapshot.universe) and all(
            has_point_in_time_membership_evidence(member) for member in snapshot.universe
        )
        manifest["row_counts"] = {ticker: values.get("row_count", 0) for ticker, values in snapshot.coverage.items()}
        failures_by_ticker = dict(getattr(snapshot, "ticker_failures", {}))
        manifest["failures_by_ticker"] = failures_by_ticker
        manifest["failures"].extend(
            f"{ticker}: {message}" for ticker, message in failures_by_ticker.items()
        )
        manifest["gaps"] = _manifest_gaps(snapshot, failures_by_ticker)
        invalid_observation_tickers = sorted(
            ticker for ticker, details in snapshot.coverage.items()
            if isinstance(details, Mapping) and details.get("invalid_observation_count", 0) > 0
        )
        if invalid_observation_tickers:
            raise ValueError(
                "invalid_observation evidence blocks refresh for: "
                + ", ".join(invalid_observation_tickers)
            )
        asset_tickers = [member.ticker for member in snapshot.universe]
        manifest["ticker_status"] = {
            ticker: (
                "failed" if ticker in failures_by_ticker else
                "insufficient_history" if snapshot.coverage.get(ticker, {}).get("row_count", 0) < MIN_PHASE3_MARKET_ROWS else
                "usable"
            )
            for ticker in asset_tickers
        }
        benchmark_status = (
            "failed" if snapshot.benchmark_ticker in failures_by_ticker else
            "insufficient_history" if snapshot.coverage.get(snapshot.benchmark_ticker, {}).get("row_count", 0) < MIN_PHASE3_MARKET_ROWS else
            "usable"
        )
        manifest["benchmark_status"] = benchmark_status
        usable_count = sum(status == "usable" for status in manifest["ticker_status"].values())
        manifest["coverage_summary"] = {
            "usable_constituents": usable_count,
            "constituent_count": len(asset_tickers),
            "usable_ratio": usable_count / len(asset_tickers) if asset_tickers else 0.0,
            "benchmark_status": benchmark_status,
        }
        manifest["panel_excluded_by_ticker"] = {
            ticker: {
                "status": "no_usable_market_history" if snapshot.coverage.get(ticker, {}).get("row_count", 0) == 0 or ticker in failures_by_ticker else "not_evaluated_due_to_coverage_policy",
                "market_row_count": snapshot.coverage.get(ticker, {}).get("row_count", 0),
                "failure": failures_by_ticker.get(ticker),
            }
            for ticker in asset_tickers if manifest["ticker_status"].get(ticker) != "usable"
        }
        if (
            not asset_tickers or
            benchmark_status != "usable" or
            usable_count / len(asset_tickers) < MIN_LIVE_COVERAGE_RATIO
        ):
            raise ValueError(
                "market coverage below minimum policy: "
                f"{usable_count}/{len(asset_tickers)} constituents usable, "
                f"benchmark={benchmark_status}, "
                f"minimum ratio={MIN_LIVE_COVERAGE_RATIO}"
            )
        observations = [bar.observation_date for bars in snapshot.bars_by_ticker.values() for bar in bars]
        if observations:
            manifest["first_observation"], manifest["last_observation"] = min(observations), max(observations)
        dataset = build_monthly_panel(snapshot, as_of=cutoff)
        latest_dataset_date = max((row.decision_date for row in dataset.rows), default="")
        manifest["current_prediction_freshness"] = [
            current_prediction_freshness(row, cutoff)
            for row in dataset.rows if row.decision_date == latest_dataset_date
        ]
        manifest["artifact_ids"]["dataset_id"] = dataset.dataset_id
        panel_tickers = {row.ticker for row in dataset.rows}
        manifest["panel_eligible_tickers"] = sorted(panel_tickers)
        manifest["panel_excluded_by_ticker"] = {
            ticker: {
                "status": "no_usable_market_history" if snapshot.coverage.get(ticker, {}).get("row_count", 0) == 0 or ticker in failures_by_ticker else "no_eligible_decision_date",
                "market_row_count": snapshot.coverage.get(ticker, {}).get("row_count", 0),
                "failure": failures_by_ticker.get(ticker),
            }
            for ticker in asset_tickers if ticker not in panel_tickers
        }
        model = SpecializedRankingModel()
        artifact = model.fit(
            dataset.rows, training_cutoff=cutoff, dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )
        backtest = walk_forward_backtest(dataset, model=model, transaction_cost_bps=10.0)
        artifact = model.artifact
        current_predictions = rank_current_candidates(dataset, artifact, as_of=cutoff)
        freshness_blocked = any(
            evidence.get("status") in {"stale", "unavailable"}
            for evidence in manifest["current_prediction_freshness"]
        )
        current_predictions_reason = None if current_predictions else (
            "no normal current predictions: latest market history is stale or unavailable; "
            "see current_prediction_freshness evidence"
            if freshness_blocked else
            "no finite model scores were available for the latest decision-date rows at or before as_of"
        )
        manifest["artifact_ids"]["model_id"] = artifact.model_id
        manifest["artifact_ids"]["backtest_id"] = backtest.backtest_id
        manifest["artifact_metadata"] = {
            "model_id": artifact.model_id,
            "dataset_id": dataset.dataset_id,
            "history_snapshot_id": dataset.history_snapshot_id,
            "current_predictions": current_predictions,
            "current_predictions_reason": current_predictions_reason,
            "current_prediction_freshness": manifest["current_prediction_freshness"],
            "descriptive_current_survivor_ranking": dataset.membership_evidence_status == "descriptive_survivor_selected_evidence",
            "performance_claims_suppressed": backtest.metrics.get("performance_claims_suppressed") is True,
        }
        report_status = backtest.metrics.get("availability_status")
        if report_status not in REFRESH_REPORT_STATUSES - {"unavailable"}:
            raise ValueError("Phase 3 refresh report status is not validated")
        report = render_phase3_report(
            dataset=dataset, model=artifact, backtest=backtest,
            current_predictions=current_predictions,
            current_predictions_reason=current_predictions_reason,
            current_predictions_as_of=cutoff,
            output_format="json" if output.suffix.lower() == ".json" else "markdown",
        )
        status = 0 if report_status in REFRESH_SUCCESS_STATUSES else 2
    except (OSError, ValueError, ProviderUnavailable, OverflowError, ZeroDivisionError) as exc:
        manifest["failures"].append(str(exc))
        report = unavailable_refresh_report(reason=str(exc), manifest=manifest, output_format="json" if output.suffix.lower() == ".json" else "markdown")
        status = 2
        report_status = "unavailable"
    output.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(report, dict):
        _write_atomic_json(output, report)
    else:
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(report, encoding="utf-8")
        temporary.replace(output)
    manifest["report_status"] = report_status
    manifest["report_path"] = str(output)
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    manifest["manifest_path"] = str(manifest_path)
    _write_atomic_json(manifest_path, manifest)
    return status
