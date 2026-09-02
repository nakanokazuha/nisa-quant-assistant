"""Typed, read-only provider boundaries for the Phase 2 evidence layer."""

from __future__ import annotations

import email.utils
import hashlib
import html
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .source_records import normalize_retrieved_at


MARKET_FIELDS = ("open", "high", "low", "close", "volume")
SUPPORTED_SEC_FORMS = frozenset({
    "3", "3/A", "4", "4/A", "5", "5/A", "SC 13D", "SC 13D/A",
    "SC 13G", "SC 13G/A", "13F-HR", "13F-HR/A", "10-K", "10-K/A",
    "10-Q", "10-Q/A", "8-K", "8-K/A",
})
SUPPORTED_FLOW_PROXIES = frozenset({"short_interest", "unusual_volume", "options_activity"})
FLOW_UNITS = frozenset({"count", "shares", "contracts", "percent", "ratio"})
RECENCY_STATUSES = frozenset({"recent", "old", "stale", "unknown"})
SENSITIVE_QUERY_PARAMETER = re.compile(
    r"(?:api[_-]?key|token|secret|password|credential)", re.IGNORECASE,
)
CONTROL_KEY = re.compile(
    r"(?:^|[_\-\s])(action|command|execute|order|trade|side|quantity|order_type|time_in_force|"
    r"authorization|recommendation|verdict|broker[_\-\s]?(?:login|credential|token|ref(?:erence)?|id))(?:$|[_\-\s])",
    re.IGNORECASE,
)
SENSITIVE_KEY = re.compile(
    r"^(?:api[_\- ]?key|access[_\- ]?token|aws[_\-]secret[_\-]access[_\-]key|"
    r"secret[_\-]access[_\-]key|password|credential)$",
    re.IGNORECASE,
)
CREDENTIAL_VALUE = re.compile(
    r"(?:api[_\- ]?key|access[_\- ]?token|token|secret|password|credential)\s*[:=]\s*(?!\[REDACTED\](?:$|[\s,&]))[^\s,&]+"
    r"|\b(?:aws[_\-]secret[_\-]access[_\-]key|secret[_\-]access[_\-]key)\s+(?!\[REDACTED\](?:$|[\s,&]))[^\s,&]+"
    r"|\bauthorization\s*[:=]\s*(?:basic|bearer|digest)\s+\S+"
    r"|\bBearer\s+\S+",
    re.IGNORECASE,
)
ORDER_VALUE = re.compile(
    r"(?:\"(?:side|order_type|quantity|symbol|time_in_force)\"\s*:|"
    r"\b(?:BUY|SELL)\s+(?:NOW|AT MARKET)\b|"
    r"\b(?:PLACE|SUBMIT|EXECUTE)\s+(?:AN?\s+)?(?:ORDER|TRADE)\b)", re.IGNORECASE,
)
DIRECTIVE_VALUE = re.compile(
    r"^\s*(?:BUY|HOLD|SELL|BUY CANDIDATE|SELL CANDIDATE|WATCH|ORDER|EXECUTE)\s*$",
    re.IGNORECASE,
)
RATING_VALUE = re.compile(
    r"(?:\b(?:analysts?|consensus)\b[^\r\n]{0,60}\b"
    r"(?:rate|rates|rated|recommend|recommends|recommended|recommendation|rating)\b[^\r\n]{0,60}\b"
    r"(?:buy|hold|sell)\b|"
    r"\b(?:rating|recommendation|consensus(?:\s+rating)?)\b[^\r\n]{0,60}\b"
    r"(?:buy|hold|sell)\b|"
    r"\b(?:buy|hold|sell)\b[^\r\n]{0,40}\b(?:rating|recommendation)\b)",
    re.IGNORECASE,
)
BROKER_REFERENCE_VALUE = re.compile(
    r"\bbroker\s+(?:ref(?:erence)?|id(?:entifier)?)\s*[:=#]?\s*[A-Z0-9][A-Z0-9_.-]*\b",
    re.IGNORECASE,
)
BROKER_IDENTIFIER_VALUE = re.compile(
    r"\b(?:"
    r"(?:broker\s+)?account\s+(?:id|identifier|number|no)"
    r"|broker\s+(?:client|customer|portfolio)\s+(?:id|identifier|number|no)"
    r"|(?:customer|portfolio)\s+(?:id|identifier|number|no)"
    r")\s*[:=#-]?\s*[A-Z0-9][A-Z0-9_.-]*\b",
    re.IGNORECASE,
)
BROKER_IDENTIFIER_KEY = re.compile(
    r"(?:broker[_\-\s]+)?(?:account|client|customer|portfolio)[_\-\s]+(?:id|identifier|number|no)",
    re.IGNORECASE,
)
ACTION_QUERY_PARAMETER = re.compile(
    r"^(?:action|command|order|order_type|quantity|side|time_in_force)$", re.IGNORECASE,
)
CONTROL_PATH = re.compile(
    r"(?:^|/)(?:order|orders|trade|trades|execute|execution)(?:[/._?]|$)", re.IGNORECASE,
)
PRIVATE_KEY_VALUE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE)
TOKEN_VALUE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_\-]{20,}|sk-(?:proj-)?[A-Za-z0-9_\-]{20,})\b")

SCAN_MAX_TEXT = 100_000
SCAN_MAX_DEPTH = 16
SCAN_MAX_WORK = 512
SCAN_MAX_NESTING = 64

ALPHA_VANTAGE_HOSTS = frozenset({"alphavantage.co", "www.alphavantage.co"})
SEC_DATA_HOSTS = frozenset({"data.sec.gov"})


class ProviderUnavailable(RuntimeError):
    """Raised when a configured read-only source cannot provide data."""


def _validate_provider_timeout(value: object) -> None:
    if isinstance(value, bool):
        raise ValueError("provider timeout must be numeric and must not be boolean")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider timeout must be numeric") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 30:
        raise ValueError("provider timeout must be greater than zero and at most 30 seconds")


@dataclass(frozen=True, slots=True)
class MarketObservation:
    ticker: str
    observation_date: str
    values: Mapping[str, object]
    currency: str
    retrieved_at: str
    citation: str
    provider: str
    provider_observation_id: str
    source_version: str
    freshness_status: str = "observed"
    units: Mapping[str, str] | None = None


def canonical_market_observation(
    *, ticker: str, observation_date: str, provider: str, provider_observation_id: str,
    field: str, value: object, currency: str, citation: str, source_version: str,
    freshness_status: str,
) -> tuple[str, str, str]:
    """Return the shared identity, content hash, and ID for a market field."""
    logical_identity = [ticker, observation_date, provider, provider_observation_id, field]
    record = [logical_identity, value, currency, citation, source_version, freshness_status]
    observation_hash = hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return (
        json.dumps(logical_identity, separators=(",", ":")),
        observation_hash,
        f"P2M-{observation_hash[:24]}",
    )


class MarketProvider(Protocol):
    def fetch_daily(
        self, tickers: Sequence[str], *, retrieved_at: str,
    ) -> Sequence[MarketObservation]:
        """Return observations for explicitly requested ticker symbols."""


@dataclass(frozen=True, slots=True)
class FixtureMarketProvider:
    path: Path
    provider: str = "fixture-market"
    source_version: str = "phase2-market-fixture-v1"

    def fetch_daily(
        self, tickers: Sequence[str], *, retrieved_at: str,
    ) -> list[MarketObservation]:
        wanted = {ticker.upper() for ticker in tickers}
        observations: list[MarketObservation] = []
        import csv

        required = (
            "ticker", "observation_date", "open", "high", "low", "close", "volume",
            "currency", "retrieved_at", "citation",
        )
        with self.path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != required:
                raise ValueError("Phase 2 market fixture must use the documented exact columns")
            for row_number, row in enumerate(reader, 2):
                if row.get(None):
                    raise ValueError(f"Phase 2 market fixture row {row_number} has extra fields")
                ticker = (row.get("ticker") or "").strip().upper()
                if ticker not in wanted:
                    continue
                observations.append(MarketObservation(
                    ticker=ticker,
                    observation_date=(row.get("observation_date") or "").strip(),
                    values={name: row.get(name) for name in MARKET_FIELDS},
                    currency=(row.get("currency") or "").strip(),
                    retrieved_at=(row.get("retrieved_at") or retrieved_at).strip(),
                    citation=(row.get("citation") or "").strip(),
                    provider=self.provider,
                    provider_observation_id=f"{self.path.name}#row-{row_number}",
                    source_version=self.source_version,
                    units={field: ("shares" if field == "volume" else "USD_per_share") for field in MARKET_FIELDS},
                ))
        return observations


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes


class ReadOnlyTransport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        """Perform one bounded read-only GET."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        raise ProviderUnavailable("redirects are not permitted for Phase 2 providers")


def _connect_to_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, port: int, timeout: float,
) -> socket.socket:
    """Connect by numeric address so the vetted DNS answer is the one used."""
    return socket.create_connection((str(address), port), timeout=timeout)


class _BoundHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection whose TCP socket is already bound to a vetted address."""

    def __init__(
        self, hostname: str, address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        *, connector: Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address, int, float], socket.socket],
        timeout: float, context: ssl.SSLContext,
    ) -> None:
        _validate_provider_timeout(timeout)
        super().__init__(hostname, timeout=timeout, context=context)
        self._bound_address = address
        self._connector = connector

    def connect(self) -> None:
        if self._tunnel_host:
            raise ProviderUnavailable("HTTPS tunneling is not permitted")
        sock: socket.socket | None = None
        try:
            sock = self._connector(self._bound_address, self.port, self.timeout)
            peer = sock.getpeername()
            peer_host = peer[0] if isinstance(peer, tuple) and peer else peer
            peer_address = ipaddress.ip_address(peer_host)
            if peer_address != self._bound_address or not peer_address.is_global:
                raise ProviderUnavailable("connected peer address was not the vetted global address")
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except ProviderUnavailable:
            if sock is not None:
                sock.close()
            raise
        except (OSError, TypeError, ValueError, ssl.SSLError) as exc:
            if sock is not None:
                sock.close()
            raise ProviderUnavailable("provider connection could not be verified") from exc


@dataclass(frozen=True, slots=True)
class UrllibReadOnlyTransport:
    allowed_hosts: frozenset[str]
    max_bytes: int = 5_000_000
    resolver: Callable[[str], Sequence[ipaddress.IPv4Address | ipaddress.IPv6Address]] | None = None
    connector: Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address, int, float], socket.socket] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.max_bytes, bool) or not isinstance(self.max_bytes, int) or self.max_bytes <= 0:
            raise ValueError("provider max_bytes must be a positive integer and must not be boolean")

    def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        try:
            # Alpha Vantage's documented API key is carried in its query
            # string. Validate authority/path and query controls separately,
            # permitting only that exact official-provider parameter.
            validation_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            _validate_public_reference(validation_url, "provider URL")
            query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if any(
                SENSITIVE_QUERY_PARAMETER.search(name) and name.lower() != "apikey"
                for name, _ in query
            ) or any(ACTION_QUERY_PARAMETER.fullmatch(name) for name, _ in query):
                raise ValueError("provider URL contains credential or control query content")
        except ValueError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        if parsed.scheme != "https" or hostname not in {
            host.lower() for host in self.allowed_hosts
        }:
            raise ProviderUnavailable("provider URL is outside the HTTPS host allowlist")
        if parsed.username or parsed.password:
            raise ProviderUnavailable("provider URL cannot contain credentials")
        _validate_provider_timeout(timeout)
        resolve = self.resolver or _resolved_addresses
        try:
            resolved = list(resolve(hostname))
            addresses = [ipaddress.ip_address(address) for address in resolved]
        except (OSError, TypeError, ValueError, socket.gaierror) as exc:
            raise ProviderUnavailable("provider host resolution failed closed") from exc
        if not addresses or any(
            (address.version == 6 and address.ipv4_mapped is not None) or not address.is_global
            for address in addresses
        ):
            raise ProviderUnavailable("provider host resolved to a non-global address")
        address = addresses[0]
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection: _BoundHTTPSConnection | None = None
        try:
            connection = _BoundHTTPSConnection(
                hostname, address, connector=self.connector or _connect_to_address,
                timeout=timeout, context=ssl.create_default_context(),
            )
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ProviderUnavailable("redirects are not permitted for Phase 2 providers")
            body = response.read(self.max_bytes + 1)
            if len(body) > self.max_bytes:
                raise ProviderUnavailable("provider response exceeds the configured byte limit")
            return HttpResponse(response.status, body)
        except ProviderUnavailable:
            raise
        except (http.client.HTTPException, TimeoutError, OSError, ssl.SSLError) as exc:
            raise ProviderUnavailable("provider request was unavailable") from exc
        finally:
            if connection is not None:
                connection.close()


@dataclass(frozen=True, slots=True)
class AlphaVantageMarketProvider:
    """Parse the documented Alpha Vantage daily OHLCV response."""

    api_key: str
    transport: ReadOnlyTransport
    source_version: str = "alpha-vantage-time-series-daily-v1"
    base_url: str = "https://www.alphavantage.co/query"
    timeout_seconds: float = 15

    def __post_init__(self) -> None:
        _validate_provider_timeout(self.timeout_seconds)

    def fetch_daily(
        self, tickers: Sequence[str], *, retrieved_at: str,
    ) -> list[MarketObservation]:
        if not self.api_key.strip():
            raise ProviderUnavailable("Alpha Vantage API key is not configured")
        validate_provider_url(self.base_url, "Alpha Vantage base_url", ALPHA_VANTAGE_HOSTS)
        results: list[MarketObservation] = []
        for ticker in tickers:
            query = urllib.parse.urlencode({
                "function": "TIME_SERIES_DAILY", "symbol": ticker, "apikey": self.api_key,
            })
            response = self.transport.get(
                f"{self.base_url}?{query}",
                headers={"Accept": "application/json", "User-Agent": "nisa-quant-phase2/1"},
                timeout=self.timeout_seconds,
            )
            if response.status_code != 200:
                raise ProviderUnavailable(f"Alpha Vantage returned HTTP {response.status_code}")
            try:
                payload = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Alpha Vantage response was not valid JSON") from exc
            series = payload.get("Time Series (Daily)") if isinstance(payload, dict) else None
            if not isinstance(series, dict):
                raise ProviderUnavailable("Alpha Vantage daily series is unavailable")
            for observation_date, values in series.items():
                if not isinstance(values, dict):
                    raise ValueError("Alpha Vantage daily observation is malformed")
                results.append(MarketObservation(
                    ticker=ticker.upper(), observation_date=str(observation_date),
                    values={
                        "open": values.get("1. open"), "high": values.get("2. high"),
                        "low": values.get("3. low"), "close": values.get("4. close"),
                        "volume": values.get("5. volume"),
                    }, currency="USD", retrieved_at=retrieved_at,
                    citation="https://www.alphavantage.co/documentation/#daily",
                    provider="alpha-vantage", provider_observation_id=f"{ticker}:{observation_date}",
                    source_version=self.source_version,
                    units={field: ("shares" if field == "volume" else "USD_per_share") for field in MARKET_FIELDS},
                ))
        return results


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_identity: str
    evidence_kind: str
    evidence_subtype: str
    source_name: str
    source_identifier: str
    source_url: str
    ticker: str | None
    issuer_cik: str | None
    publication_at: str | None
    period_start: str | None
    period_end: str | None
    fact_field: str | None
    fact_value: str | None
    fact_unit: str | None
    topic: str
    evidence_text: str | None
    source_quality: str
    recency_status: str
    corroboration_status: str
    uncertainty_status: str
    metadata: Mapping[str, object] = field(default_factory=dict)
    retrieved_at: str = ""
    source_version: str = ""
    citation: str = ""


@dataclass(frozen=True, slots=True)
class RssSourceConfig:
    source_name: str
    source_url: str
    source_version: str
    terms_url: str
    entity_aliases: Mapping[str, Sequence[str]]
    content_policy: str
    source_quality: str = "configured_public_feed"
    topic: str = "news"


@dataclass(frozen=True, slots=True)
class FlowProxyObservation:
    ticker: str
    proxy_type: str
    observed_at: str
    value: str
    unit: str
    source_name: str
    source_url: str
    citation: str
    retrieved_at: str
    source_version: str


def _legacy_ipv4_component(value: str) -> int | None:
    if not value:
        return None
    lowered = value.lower()
    if lowered.startswith("0x"):
        digits, base = lowered[2:], 16
    elif len(value) > 1 and value.startswith("0"):
        digits, base = value[1:], 8
    elif value.isdigit():
        digits, base = value, 10
    else:
        return None
    if not digits:
        return 0
    try:
        return int(digits, base)
    except ValueError:
        return None


def _legacy_ipv4_address(hostname: str) -> ipaddress.IPv4Address | None:
    parts = hostname.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values = [_legacy_ipv4_component(part) for part in parts]
    if any(value is None for value in values):
        return None
    numbers = [int(value) for value in values]
    limits = {1: (2**32,), 2: (2**8, 2**24), 3: (2**8, 2**8, 2**16), 4: (2**8,) * 4}[len(numbers)]
    if any(value >= limit for value, limit in zip(numbers, limits)):
        return None
    if len(numbers) == 1:
        packed = numbers[0]
    elif len(numbers) == 2:
        packed = (numbers[0] << 24) | numbers[1]
    elif len(numbers) == 3:
        packed = (numbers[0] << 24) | (numbers[1] << 16) | numbers[2]
    else:
        packed = (numbers[0] << 24) | (numbers[1] << 16) | (numbers[2] << 8) | numbers[3]
    return ipaddress.IPv4Address(packed)


def _resolved_addresses(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        results = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        raise ValueError("host resolution failed closed") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for result in results:
        try:
            addresses.append(ipaddress.ip_address(result[4][0]))
        except (IndexError, ValueError):
            raise ValueError("host resolution returned an invalid address")
    if not addresses:
        raise ValueError("host resolution returned no addresses")
    return addresses


def _validate_public_reference(
    value: str, label: str, *, allow_fixture: bool = False, resolve_host: bool = False,
) -> None:
    if allow_fixture and value.startswith("fixture:") and len(value) > len("fixture:"):
        return
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{label} must be an HTTPS reference without credentials")
    hostname = parsed.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    legacy_address = _legacy_ipv4_address(hostname)
    if address is not None or legacy_address is not None or hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError(f"{label} cannot target an IP literal or loopback host")
    if resolve_host:
        resolved = _resolved_addresses(hostname)
        if any(address.version == 6 and address.ipv4_mapped is not None for address in resolved):
            raise ValueError(f"{label} resolved to an IPv4-mapped address")
        if any(not address.is_global for address in resolved):
            raise ValueError(f"{label} resolved to a non-global address")
    decoded_paths, paths_truncated = _scan_candidates_with_status(parsed.path)
    if paths_truncated:
        raise ValueError(f"{label} exceeds the bounded decoding limit")
    decoded_queries: list[tuple[str, str]] = []
    query_candidates, query_truncated = _scan_candidates_with_status(parsed.query)
    if query_truncated:
        raise ValueError(f"{label} exceeds the bounded decoding limit")
    for query in query_candidates:
        decoded_queries.extend(urllib.parse.parse_qsl(query, keep_blank_values=True))
    if any(SENSITIVE_QUERY_PARAMETER.search(key) for key, _ in decoded_queries):
        raise ValueError(f"{label} cannot contain credential-like query parameters")
    if any(ACTION_QUERY_PARAMETER.fullmatch(key) for key, _ in decoded_queries):
        raise ValueError(f"{label} contains control content")
    if any(
        re.search(r"(?:submit[-_]?order|execute[-_]?trade|broker/login|(?:^|/)(?:order|orders|trade|trades|execute|execution)(?:[/._?-]|$))", path, re.IGNORECASE)
        for path in decoded_paths
    ):
        raise ValueError(f"{label} contains control content")


def validate_public_reference(
    value: str, label: str, *, allow_fixture: bool = False, resolve_host: bool = False,
) -> None:
    """Validate a public HTTPS reference, optionally resolving its host fail closed."""
    _validate_public_reference(value, label, allow_fixture=allow_fixture, resolve_host=resolve_host)


def _normalize_ticker(value: object) -> str:
    ticker = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
        raise ValueError("ticker is not a typed US symbol")
    return ticker


def _normalize_cik(value: str) -> str:
    digits = str(value).strip()
    if not digits.isdigit() or len(digits) > 10:
        raise ValueError("SEC CIK must contain at most 10 digits")
    return digits.zfill(10)


def validate_provider_url(value: str, label: str, allowed_hosts: frozenset[str]) -> None:
    """Validate an endpoint against a named provider's fixed official hosts."""
    _validate_public_reference(value, label)
    hostname = (urllib.parse.urlsplit(value).hostname or "").lower().rstrip(".")
    if hostname not in allowed_hosts:
        raise ValueError(f"{label} is outside the official provider host allowlist")


def _publication_at(value: object) -> str:
    if isinstance(value, str) and value.strip():
        try:
            return normalize_retrieved_at(value)
        except ValueError:
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("publication date is not ISO formatted") from exc
            return datetime.combine(parsed, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    raise ValueError("publication date is required")


def _recency(publication_at: str, retrieved_at: str) -> str:
    publication = datetime.fromisoformat(publication_at)
    retrieved = datetime.fromisoformat(retrieved_at)
    if retrieved < publication:
        raise ValueError("evidence retrieval cannot precede publication")
    return "recent" if (retrieved - publication).days <= 30 else "old"


def _form_subtype(form: str) -> tuple[str, str]:
    if form in {"3", "3/A", "4", "4/A", "5", "5/A"}:
        return f"form_{form[0]}", "insider_activity"
    if form.startswith("SC 13D"):
        return "schedule_13d", "beneficial_ownership"
    if form.startswith("SC 13G"):
        return "schedule_13g", "beneficial_ownership"
    if form.startswith("13F-HR"):
        return "form_13f_hr", "institutional_holdings"
    return form.lower().replace("/", "_"), "filing"


def normalize_sec_submissions(
    payload: Mapping[str, object], *, ticker: str, cik: str, retrieved_at: str,
    source_url: str = "https://data.sec.gov/submissions/", issues: list[str] | None = None,
) -> list[EvidenceRecord]:
    normalized_cik = _normalize_cik(cik)
    normalized_ticker = _normalize_ticker(ticker)
    normalized_retrieved = normalize_retrieved_at(retrieved_at)
    validate_provider_url(source_url, "SEC submissions source_url", SEC_DATA_HOSTS)
    filings = payload.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        raise ValueError("SEC submissions payload is missing filings.recent")
    names = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument")
    columns = {name: recent.get(name) for name in names}
    acceptance_times = recent.get("acceptanceDateTime")
    if acceptance_times is not None:
        columns["acceptanceDateTime"] = acceptance_times
    if any(not isinstance(value, list) for value in columns.values()):
        raise ValueError("SEC submissions payload has incomplete recent filing columns")
    length = len(columns["accessionNumber"])
    if any(len(value) != length for value in columns.values()):
        raise ValueError("SEC submissions payload columns have inconsistent lengths")
    records: list[EvidenceRecord] = []
    for index in range(length):
        accession = str(columns["accessionNumber"][index]).strip()
        form = str(columns["form"][index]).strip()
        if form not in SUPPORTED_SEC_FORMS:
            if issues is not None:
                issues.append(f"unsupported SEC form: {form}")
            continue
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            raise ValueError("SEC accession number is malformed")
        if accession[:10] != normalized_cik:
            raise ValueError("SEC accession CIK does not match the explicit target CIK")
        acceptance_time = columns.get("acceptanceDateTime", [None] * length)[index]
        filed = _publication_at(acceptance_time or columns["filingDate"][index])
        report_date = columns["reportDate"][index]
        period_end = str(report_date).strip() if isinstance(report_date, str) and report_date.strip() else None
        if period_end is not None:
            try:
                if date.fromisoformat(period_end).isoformat() != period_end:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("SEC reportDate is malformed") from exc
            if date.fromisoformat(period_end) > date.fromisoformat(filed[:10]):
                raise ValueError("SEC reportDate cannot follow publication date")
        subtype, topic = _form_subtype(form)
        lag_days = None if period_end is None else (date.fromisoformat(filed[:10]) - date.fromisoformat(period_end)).days
        document = str(columns["primaryDocument"][index]).strip()
        if not document:
            raise ValueError("SEC primary document is required")
        citation = f"https://www.sec.gov/Archives/edgar/data/{int(normalized_cik)}/{accession.replace('-', '')}/{document}"
        records.append(EvidenceRecord(
            evidence_identity=f"sec-filing:{normalized_cik}:{accession}",
            evidence_kind="alert" if topic != "filing" else "filing", evidence_subtype=subtype,
            source_name="SEC EDGAR", source_identifier=accession, source_url=source_url,
            ticker=normalized_ticker, issuer_cik=normalized_cik, publication_at=filed,
            period_start=None, period_end=period_end, fact_field="form", fact_value=form,
            fact_unit=None, topic=topic, evidence_text=None, source_quality="authoritative_regulatory",
            recency_status=_recency(filed, normalized_retrieved), corroboration_status="single_source",
            uncertainty_status="filing_lag" if lag_days and lag_days > 0 else "none",
            metadata={"filing_lag_days": lag_days, "primary_document": document},
            retrieved_at=normalized_retrieved, source_version="sec-submissions-v1", citation=citation,
        ))
    return records


def normalize_sec_company_facts(
    payload: Mapping[str, object], *, ticker: str, cik: str, retrieved_at: str,
    source_url: str = "https://data.sec.gov/api/xbrl/companyfacts/", issues: list[str] | None = None,
) -> list[EvidenceRecord]:
    normalized_cik = _normalize_cik(cik)
    normalized_ticker = _normalize_ticker(ticker)
    normalized_retrieved = normalize_retrieved_at(retrieved_at)
    validate_provider_url(source_url, "SEC Company Facts source_url", SEC_DATA_HOSTS)
    payload_cik = payload.get("cik")
    try:
        if isinstance(payload_cik, bool) or not isinstance(payload_cik, (int, str)):
            raise ValueError
        normalized_payload_cik = _normalize_cik(str(payload_cik))
    except ValueError:
        issue = "Company Facts top-level CIK is missing or malformed"
        if payload_cik is not None:
            issue = "Company Facts top-level CIK is malformed"
        if issues is not None:
            issues.append(issue)
            return []
        raise ValueError(issue)
    if normalized_payload_cik != normalized_cik:
        issue = "Company Facts top-level CIK does not match the explicit target CIK"
        if issues is not None:
            issues.append(issue)
            return []
        raise ValueError(issue)
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        raise ValueError("SEC company facts payload is missing facts")
    records: list[EvidenceRecord] = []
    for taxonomy, taxonomy_facts in facts.items():
        if not isinstance(taxonomy_facts, dict):
            continue
        for fact_name, fact in taxonomy_facts.items():
            units = fact.get("units") if isinstance(fact, dict) else None
            if not isinstance(units, dict):
                continue
            for unit, entries in units.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    if "val" not in entry or "filed" not in entry or "end" not in entry:
                        if issues is not None:
                            issues.append(f"incomplete Company Facts entry: {fact_name}")
                        continue
                    accession = str(entry.get("accn", "")).strip()
                    if accession and (
                        not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or accession[:10] != normalized_cik
                    ):
                        if issues is not None:
                            issues.append(f"Company Facts accession CIK mismatch: {accession}")
                        continue
                    try:
                        if isinstance(entry["val"], bool):
                            raise TypeError
                        numeric = float(entry["val"])
                    except (TypeError, ValueError):
                        if issues is not None:
                            issues.append(f"nonnumeric Company Facts value: {fact_name}")
                        continue
                    if not math.isfinite(numeric):
                        if issues is not None:
                            issues.append(f"nonfinite Company Facts value: {fact_name}")
                        continue
                    filed = _publication_at(entry["filed"])
                    period_end = entry["end"]
                    try:
                        valid_period_end = isinstance(period_end, str) and date.fromisoformat(period_end).isoformat() == period_end
                    except ValueError:
                        valid_period_end = False
                    if not valid_period_end:
                        if issues is not None:
                            issues.append(f"malformed Company Facts period: {fact_name}")
                        continue
                    period_start = entry.get("start")
                    if period_start is not None:
                        try:
                            valid_period_start = isinstance(period_start, str) and date.fromisoformat(period_start).isoformat() == period_start
                        except ValueError:
                            valid_period_start = False
                        if not valid_period_start:
                            if issues is not None:
                                issues.append(f"malformed Company Facts start: {fact_name}")
                            continue
                        if period_start > period_end:
                            if issues is not None:
                                issues.append(f"Company Facts period starts after it ends: {fact_name}")
                            continue
                    if date.fromisoformat(period_end) > date.fromisoformat(filed[:10]):
                        if issues is not None:
                            issues.append(f"Company Facts period follows publication: {fact_name}")
                        continue
                    form = str(entry.get("form", "")).strip()
                    if form and form not in SUPPORTED_SEC_FORMS:
                        if issues is not None:
                            issues.append(f"unsupported Company Facts form: {form}")
                        continue
                    citation = source_url
                    if accession:
                        citation = f"https://www.sec.gov/Archives/edgar/data/{int(normalized_cik)}/{accession.replace('-', '')}/"
                    identity = (
                        f"sec-fact:{normalized_cik}:{accession}:{fact_name}:{unit}:"
                        f"{period_start or ''}:{period_end}:{entry.get('frame', '')}"
                    )
                    records.append(EvidenceRecord(
                        evidence_identity=identity, evidence_kind="filing", evidence_subtype="company_fact",
                        source_name="SEC XBRL Company Facts", source_identifier=accession or f"{fact_name}:{period_end}",
                        source_url=source_url, ticker=normalized_ticker, issuer_cik=normalized_cik,
                        publication_at=filed, period_start=period_start, period_end=period_end,
                        fact_field=str(fact_name), fact_value=str(entry["val"]), fact_unit=str(unit),
                        topic="reported_fact", evidence_text=None, source_quality="authoritative_regulatory",
                        recency_status=_recency(filed, normalized_retrieved), corroboration_status="single_source",
                        uncertainty_status="reported_fact", metadata={"taxonomy": taxonomy, "form": form, "frame": entry.get("frame")},
                        retrieved_at=normalized_retrieved, source_version="sec-companyfacts-v1", citation=citation,
                    ))
    return records


@dataclass(frozen=True, slots=True)
class SECEdgarProvider:
    """Read-only adapter for SEC submissions and Company Facts JSON."""

    transport: ReadOnlyTransport
    user_agent: str
    source_version: str = "sec-edgar-phase2-v1"
    submissions_base_url: str = "https://data.sec.gov/submissions/"
    companyfacts_base_url: str = "https://data.sec.gov/api/xbrl/companyfacts/"
    timeout_seconds: float = 20
    limiter: "RequestRateLimiter | None" = None

    def __post_init__(self) -> None:
        _validate_provider_timeout(self.timeout_seconds)
        validate_provider_url(self.submissions_base_url, "SEC submissions base_url", SEC_DATA_HOSTS)
        validate_provider_url(self.companyfacts_base_url, "SEC Company Facts base_url", SEC_DATA_HOSTS)

    def _get_json(self, url: str) -> Mapping[str, object]:
        if not self.user_agent.strip():
            raise ProviderUnavailable("SEC User-Agent is not configured")
        if self.limiter is not None:
            self.limiter.wait()
        response = self.transport.get(
            url, headers={"Accept": "application/json", "User-Agent": self.user_agent}, timeout=self.timeout_seconds,
        )
        if response.status_code != 200:
            raise ProviderUnavailable(f"SEC returned HTTP {response.status_code}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("SEC response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("SEC response must be a JSON object")
        return payload

    def fetch_submissions(self, cik: str, *, ticker: str, retrieved_at: str, issues: list[str] | None = None) -> list[EvidenceRecord]:
        normalized_cik = _normalize_cik(cik)
        payload = self._get_json(f"{self.submissions_base_url.rstrip('/')}/CIK{normalized_cik}.json")
        return normalize_sec_submissions(
            payload, ticker=ticker, cik=normalized_cik, retrieved_at=retrieved_at,
            source_url=self.submissions_base_url, issues=issues,
        )

    def fetch_company_facts(self, cik: str, *, ticker: str, retrieved_at: str, issues: list[str] | None = None) -> list[EvidenceRecord]:
        normalized_cik = _normalize_cik(cik)
        payload = self._get_json(f"{self.companyfacts_base_url.rstrip('/')}/CIK{normalized_cik}.json")
        return normalize_sec_company_facts(
            payload, ticker=ticker, cik=normalized_cik, retrieved_at=retrieved_at,
            source_url=self.companyfacts_base_url, issues=issues,
        )


@dataclass(frozen=True, slots=True)
class RssFeedProvider:
    transport: ReadOnlyTransport
    config: RssSourceConfig
    timeout_seconds: float = 15

    def __post_init__(self) -> None:
        _validate_provider_timeout(self.timeout_seconds)

    def fetch(self, *, retrieved_at: str) -> list[EvidenceRecord]:
        # The production transport resolves once and binds the TCP connection
        # to that vetted address. A generic injected transport keeps the
        # fail-closed validation seam for callers that supply their own
        # network boundary.
        _validate_public_reference(
            self.config.source_url, "RSS source_url",
            resolve_host=not isinstance(self.transport, UrllibReadOnlyTransport),
        )
        response = self.transport.get(
            self.config.source_url, headers={"Accept": "application/rss+xml, application/xml", "User-Agent": "nisa-quant-phase2/1"},
            timeout=self.timeout_seconds,
        )
        if response.status_code != 200:
            raise ProviderUnavailable(f"RSS returned HTTP {response.status_code}")
        return normalize_rss_feed(response.body, config=self.config, retrieved_at=retrieved_at)


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _rss_text(item: ET.Element, name: str) -> str:
    for child in item:
        if _xml_local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _match_ticker(text: str, aliases: Mapping[str, Sequence[str]]) -> str:
    matches = {
        _normalize_ticker(ticker)
        for ticker, values in aliases.items()
        for alias in values
        if alias and re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", text, re.IGNORECASE)
    }
    if len(matches) != 1:
        raise ValueError("RSS article ticker mapping is ambiguous or unavailable")
    return next(iter(matches))


def normalize_rss_feed(
    xml_bytes: bytes, *, config: RssSourceConfig, retrieved_at: str,
) -> list[EvidenceRecord]:
    if config.content_policy not in {"metadata_only", "bounded_excerpt"}:
        raise ValueError("RSS content policy must be metadata_only or bounded_excerpt")
    if not config.source_name.strip() or not config.source_version.strip():
        raise ValueError("RSS source identity is incomplete")
    _validate_public_reference(config.source_url, "RSS source_url")
    _validate_public_reference(config.terms_url, "RSS terms_url")
    for ticker in config.entity_aliases:
        _normalize_ticker(ticker)
    normalized_retrieved = normalize_retrieved_at(retrieved_at)
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError("RSS response was not valid XML") from exc
    records: list[EvidenceRecord] = []
    for item in root.iter():
        if _xml_local_name(item.tag) != "item":
            continue
        guid = _rss_text(item, "guid") or _rss_text(item, "link")
        link = _rss_text(item, "link")
        title = _rss_text(item, "title")
        summary = _rss_text(item, "description")
        published = _rss_text(item, "pubDate")
        if not guid or not link or not title or not published:
            raise ValueError("RSS article is missing identity, link, title, or publication time")
        _validate_public_reference(link, "RSS article link")
        try:
            parsed_publication = email.utils.parsedate_to_datetime(published)
        except (TypeError, ValueError, IndexError) as exc:
            raise ValueError("RSS publication time is malformed") from exc
        if parsed_publication is None:
            raise ValueError("RSS publication time is malformed")
        if parsed_publication.tzinfo is None:
            parsed_publication = parsed_publication.replace(tzinfo=timezone.utc)
        publication_at = parsed_publication.astimezone(timezone.utc).isoformat()
        ticker = _match_ticker(f"{title} {summary}", config.entity_aliases)
        text = title if config.content_policy == "metadata_only" else f"{title}\n{summary}"[:1000]
        records.append(EvidenceRecord(
            evidence_identity=f"rss:{config.source_name}:{guid}", evidence_kind="news",
            evidence_subtype="rss_article", source_name=config.source_name, source_identifier=guid,
            source_url=config.source_url, ticker=ticker, issuer_cik=None, publication_at=publication_at,
            period_start=None, period_end=None, fact_field=None, fact_value=None, fact_unit=None,
            topic=config.topic, evidence_text=text, source_quality=config.source_quality,
            recency_status=_recency(publication_at, normalized_retrieved), corroboration_status="uncorroborated",
            uncertainty_status=config.content_policy, metadata={"terms_url": config.terms_url, "article_url": link},
            retrieved_at=normalized_retrieved, source_version=config.source_version, citation=link,
        ))
    if not records:
        raise ValueError("RSS response contains no articles")
    return records


def normalize_flow_proxy(record: FlowProxyObservation) -> EvidenceRecord:
    if record.proxy_type not in SUPPORTED_FLOW_PROXIES:
        raise ValueError("unsupported flow proxy; use the observable source field name")
    ticker = _normalize_ticker(record.ticker)
    if record.unit not in FLOW_UNITS:
        raise ValueError("flow proxy unit is unsupported")
    _validate_public_reference(record.source_url, "flow source_url")
    _validate_public_reference(record.citation, "flow citation", allow_fixture=True)
    if not record.source_name.strip() or not record.source_version.strip() or not record.citation.strip():
        raise ValueError("flow proxy provenance is incomplete")
    observed_at = _publication_at(record.observed_at)
    retrieved_at = normalize_retrieved_at(record.retrieved_at)
    _recency(observed_at, retrieved_at)
    try:
        if isinstance(record.value, bool):
            raise TypeError
        numeric = float(record.value)
    except (TypeError, ValueError) as exc:
        raise ValueError("flow proxy value must be numeric") from exc
    if not math.isfinite(numeric):
        raise ValueError("flow proxy value must be finite")
    observed_date = date.fromisoformat(observed_at[:10])
    return EvidenceRecord(
        evidence_identity=f"flow:{record.source_name}:{ticker}:{record.proxy_type}:{observed_date.isoformat()}",
        evidence_kind="alert", evidence_subtype="flow_proxy", source_name=record.source_name,
        source_identifier=f"{ticker}:{record.proxy_type}:{observed_date.isoformat()}", source_url=record.source_url,
        ticker=ticker, issuer_cik=None, publication_at=observed_at, period_start=None,
        period_end=observed_date.isoformat(), fact_field=record.proxy_type, fact_value=record.value,
        fact_unit=record.unit, topic="large_flow_proxy", evidence_text=None,
        source_quality="configured_provider", recency_status=_recency(observed_at, retrieved_at),
        corroboration_status="single_source", uncertainty_status="proxy_observable_only",
        metadata={"observable": record.proxy_type}, retrieved_at=retrieved_at,
        source_version=record.source_version, citation=record.citation,
    )


def _scan_normalize(value: str) -> str:
    return "".join(
        "-" if unicodedata.category(character) == "Pd" else character
        for character in unicodedata.normalize("NFKC", value)
        if unicodedata.category(character) != "Cf"
    )


def _iterative_percent_decode(value: str) -> tuple[str, ...]:
    values = [value]
    for _ in range(6):
        decoded = urllib.parse.unquote(values[-1])
        if len(decoded) > 100_000:
            break
        if decoded == values[-1]:
            break
        values.append(decoded)
    return tuple(values)


def _url_contains_control_content(value: str) -> bool:
    candidates, truncated = _scan_candidates_with_status(value)
    if truncated:
        return True
    for candidate in candidates:
        try:
            parsed = urllib.parse.urlsplit(candidate)
        except ValueError:
            continue
        if not (parsed.scheme and parsed.netloc):
            continue
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if len(query) > SCAN_MAX_WORK:
            return True
        for name, query_value in query:
            name_candidates, name_truncated = _scan_candidates_with_status(name)
            value_candidates, value_truncated = _scan_candidates_with_status(query_value)
            if name_truncated or value_truncated:
                return True
            if any(
                SENSITIVE_QUERY_PARAMETER.search(decoded_name)
                or ACTION_QUERY_PARAMETER.fullmatch(decoded_name)
                for decoded_name in name_candidates
            ):
                return True
            if any(
                CREDENTIAL_VALUE.search(decoded_value)
                or ORDER_VALUE.search(decoded_value)
                or RATING_VALUE.search(decoded_value)
                or DIRECTIVE_VALUE.fullmatch(decoded_value.strip())
                for decoded_value in value_candidates
            ):
                return True
        if CONTROL_PATH.search(parsed.path):
            return True
    return False


def _scan_candidates_with_status(value: str) -> tuple[tuple[str, ...], bool]:
    candidates: list[str] = []
    normalized_value = _scan_normalize(value)
    truncated = len(normalized_value) > SCAN_MAX_TEXT
    pending: list[tuple[str, int]] = [(normalized_value[:SCAN_MAX_TEXT], 0)]
    seen: set[str] = set()
    work = 0
    while pending and work < SCAN_MAX_WORK:
        work += 1
        if not pending:
            break
        current, depth = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        candidates.append(current)
        if depth >= SCAN_MAX_DEPTH:
            if urllib.parse.unquote(current) != current or html.unescape(current) != current:
                truncated = True
            continue
        for decoded in (urllib.parse.unquote(current), html.unescape(current)):
            normalized = _scan_normalize(decoded)
            if len(normalized) > SCAN_MAX_TEXT:
                truncated = True
                continue
            if normalized not in seen:
                pending.append((normalized, depth + 1))
    if pending:
        truncated = True
    return tuple(candidates), truncated


def _scan_candidates(value: str) -> tuple[str, ...]:
    """Return bounded decode candidates, retaining the legacy private helper API."""
    return _scan_candidates_with_status(value)[0]


def contains_control_content(value: object, *, key: str = "", _depth: int = 0) -> bool:
    """Identify control material in every recursively supplied evidence value."""
    if _depth >= SCAN_MAX_NESTING:
        return True
    key_candidates, key_truncated = _scan_candidates_with_status(str(key))
    if key_truncated:
        return True
    for normalized_key in key_candidates:
        if normalized_key == "no_verdict_boundary":
            continue
        if (
            CONTROL_KEY.search(normalized_key)
            or re.search(r"(?:^|[_\-\s])rating(?:$|[_\-\s])", normalized_key, re.IGNORECASE)
        ):
            return True
    if isinstance(value, Mapping):
        if len(value) > SCAN_MAX_WORK:
            return True
        for child_key, child in value.items():
            child_key_text = str(child_key)
            child_key_candidates, child_key_truncated = _scan_candidates_with_status(child_key_text)
            if child_key_truncated:
                return True
            for normalized_child in child_key_candidates:
                if SENSITIVE_KEY.fullmatch(normalized_child) and not (
                    isinstance(child, str)
                    and any(candidate.strip() == "[REDACTED]" for candidate in _scan_candidates(child))
                ):
                    return True
                if BROKER_IDENTIFIER_KEY.fullmatch(normalized_child) and not (
                    isinstance(child, str)
                    and any(candidate.strip() == "[REDACTED]" for candidate in _scan_candidates(child))
                ):
                    return True
            if contains_control_content(child, key=child_key_text, _depth=_depth + 1):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > SCAN_MAX_WORK:
            return True
        return any(contains_control_content(child, _depth=_depth + 1) for child in value)
    if isinstance(value, str):
        candidates, truncated = _scan_candidates_with_status(value)
        for candidate in candidates:
            if candidate.strip() == "[REDACTED]":
                continue
            if _url_contains_control_content(candidate):
                return True
            if (
                CREDENTIAL_VALUE.search(candidate)
                or ORDER_VALUE.search(candidate)
                or RATING_VALUE.search(candidate)
                or BROKER_REFERENCE_VALUE.search(candidate)
                or BROKER_IDENTIFIER_VALUE.search(candidate)
                or PRIVATE_KEY_VALUE.search(candidate)
                or TOKEN_VALUE.search(candidate)
                or DIRECTIVE_VALUE.fullmatch(candidate)
            ):
                return True
        return truncated
    if isinstance(value, (bytes, bytearray)):
        return True
    return False


@dataclass
class RequestRateLimiter:
    """Small local limiter used before each SEC request."""

    max_requests_per_second: float
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    _next_allowed: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.max_requests_per_second, bool):
            raise ValueError("SEC max_requests_per_second must be numeric and must not be boolean")
        try:
            valid_rate = math.isfinite(self.max_requests_per_second)
        except TypeError as exc:
            raise ValueError("SEC max_requests_per_second must be numeric") from exc
        if not valid_rate or not 0 < self.max_requests_per_second <= 10:
            raise ValueError("SEC max_requests_per_second must be greater than zero and at most 10")

    def wait(self) -> None:
        now = self.clock()
        if self._next_allowed is not None and now < self._next_allowed:
            self.sleeper(self._next_allowed - now)
            now = self.clock()
        self._next_allowed = now + (1.0 / self.max_requests_per_second)
