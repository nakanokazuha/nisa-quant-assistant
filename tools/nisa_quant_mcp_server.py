"""Safe local MCP adapter for the existing Phase 3 NISA quant producer.

The adapter intentionally owns no quant logic, network client, cache selector,
or broker capability.  It validates a small request schema and delegates the
actual refresh to :func:`nisa_quant.refresh_pipeline.refresh_phase3`.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Literal


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from nisa_quant.refresh_pipeline import refresh_phase3  # noqa: E402


Mode = Literal["live", "replay"]
ReportFormat = Literal["markdown", "json"]
TOOL_NAMES = ("nisa_quant_refresh", "nisa_quant_latest")
PHASE3_CACHE_DIR = (REPO_ROOT / "data" / "phase3").resolve()
REPORT_DIR = (REPO_ROOT / "reports" / "phase3").resolve()
REPORT_RELATIVE_DIR = Path("reports") / "phase3"
LATEST_REPORT_PATHS = (REPORT_DIR / "hermes-report.json", REPORT_DIR / "hermes-report.markdown")
MAX_LIVE_LIMIT = 50
MAX_REPORT_CONTENT = 16_000
MAX_FAILURES = 50
MAX_GAPS = 50
MAX_OUTPUT_STRING = 4_096
MAX_OUTPUT_COLLECTION_ITEMS = 50
MAX_OUTPUT_DEPTH = 6
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SECRET_KEY_FRAGMENT_PATTERN = r"(?:password|passwd|secret|token|api[_-]?key|credential|authorization|bearer|private[_-]?key)"
_SECRET_KEY_PATTERN = _SECRET_KEY_FRAGMENT_PATTERN
_SECRET_ASSIGNMENT_KEY_PATTERN = (
    rf"(?:{_SECRET_KEY_PATTERN}|[A-Za-z0-9_.-]*{_SECRET_KEY_FRAGMENT_PATTERN}[A-Za-z0-9_.-]*)"
)
_SECRET_PATTERN = re.compile(_SECRET_KEY_PATTERN, re.IGNORECASE)
_LOCAL_PATH_WITH_SPACES_PATTERN = re.compile(
    rf"(?P<public_url>https?://[^\s\"'<>]+)|"
    r"(?P<file_url>file://(?:localhost)?/[A-Za-z0-9._~+-]+(?:/[A-Za-z0-9._~+-]+)+"
    r"(?:\s+[A-Za-z0-9._~+-]*[/\.][A-Za-z0-9._~+-]+)+)|"
    r"(?P<local_path>(?<![\w./-])/[A-Za-z0-9._~+-]+"
    r"(?:/[A-Za-z0-9._~+-]+)+(?:\s+[A-Za-z0-9._~+-]*[/\.][A-Za-z0-9._~+-]+)+)",
    re.IGNORECASE,
)
_LOCAL_PATH_PATTERN = re.compile(
    r"(?P<public_url>https?://[^\s\"'<>]+)|"
    r"(?P<file_url>file://(?:localhost)?/[^\s\"'<>;,)\]}]+)|"
    r"(?P<local_path>(?<![\w./-])/(?!/)[^\s\"'<>;,)\]}]+)",
    re.IGNORECASE,
)
_SECRET_BLOCK_ASSIGNMENT_PATTERN = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?:\\?[\"'])?{_SECRET_ASSIGNMENT_KEY_PATTERN}(?:\\?[\"'])?\s*[:=]\s*)"
    r"\|[0-9+-]*[ \t]*(?:\r?\n[ \t]+[^\r\n]*)+",
    re.IGNORECASE,
)
_SECRET_QUOTED_ASSIGNMENT_PATTERNS = (
    re.compile(
        rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?:\\?[\"'])?{_SECRET_ASSIGNMENT_KEY_PATTERN}(?:\\?[\"'])?\s*[:=]\s*)"
        r'"(?P<value>(?:\\.|[^"\\])*)(?<!\\)"',
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?:\\?[\"'])?{_SECRET_ASSIGNMENT_KEY_PATTERN}(?:\\?[\"'])?\s*[:=]\s*)"
        r"'(?P<value>(?:\\.|[^'\\])*)(?<!\\)'",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?:\\?[\"'])?{_SECRET_ASSIGNMENT_KEY_PATTERN}(?:\\?[\"'])?\s*[:=]\s*)"
        r'\\"(?P<value>(?:\\.|[^"\\])*)\\"(?=\s*(?:[,}\]]|[;]|$))',
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?:\\?[\"'])?{_SECRET_ASSIGNMENT_KEY_PATTERN}(?:\\?[\"'])?\s*[:=]\s*)"
        r"\\'(?P<value>(?:\\.|[^'\\])*)\\'(?=\s*(?:[,}\]]|[;]|$))",
        re.IGNORECASE,
    ),
)
_SECRET_COLLECTION_ASSIGNMENT_PATTERN = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?:\\?[\"'])?{_SECRET_ASSIGNMENT_KEY_PATTERN}(?:\\?[\"'])?\s*[:=]\s*)"
    r"(?P<value>\[[^\r\n]*?\]|\{[^\r\n]*?\})"
    r"(?=\s*(?:[\"']\s*)?(?:[,}]|$))",
    re.IGNORECASE,
)
_SECRET_UNQUOTED_ASSIGNMENT_PATTERN = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?:\\?[\"'])?{_SECRET_ASSIGNMENT_KEY_PATTERN}(?:\\?[\"'])?\s*[:=]\s*)"
    r"(?P<value>[^\r\n,;\[\]}\"']+?)(?=\s*(?:[,}\]]|$))",
    re.IGNORECASE,
)
_AUTHORIZATION_BEARER_PATTERN = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?:\\?[\"'])?authorization(?:\\?[\"'])?\s*[:=]\s*)(?:bearer\s+)?"
    r"(?P<value>[^\r\n,;\[\]}\"']+?)(?=\s*(?:[,}\]]|$))",
    re.IGNORECASE,
)
_SECRET_SHELL_ASSIGNMENT_PATTERN = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?:\\?[\"'])?{_SECRET_ASSIGNMENT_KEY_PATTERN}(?:\\?[\"'])?\s*=\s*)"
    r"(?P<value>[^\s;]+)",
    re.IGNORECASE,
)
_SECRET_YAML_ASSIGNMENT_PATTERN = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?<![\"']){_SECRET_ASSIGNMENT_KEY_PATTERN}\s*:\s*)"
    r"(?P<value>[^\r\n]+?)(?=\r?\n|$)",
    re.IGNORECASE,
)
_LOCAL_PATH_MARKER = "[LOCAL_PATH_REDACTED]"
_SECRET_MARKER = "[SECRET_REDACTED]"
_OUTPUT_TRUNCATION_MARKER = "...[TRUNCATED]"
_OUTPUT_DEPTH_MARKER = "[NESTED_VALUE_TRUNCATED]"


def _replace_local_path(match: re.Match[str]) -> str:
    return match.group("public_url") or _LOCAL_PATH_MARKER


def _replace_quoted_secret_assignment(match: re.Match[str], quote: str) -> str:
    prefix = match.group("prefix").replace('\\"', '"').replace("\\'", "'")
    return prefix + quote + _SECRET_MARKER + quote


def _relative_path(path: Path) -> str:
    """Return a stable repo-local display path without exposing host paths."""
    return (REPORT_RELATIVE_DIR / path.name).as_posix()


def _manifest_path(report_path: Path) -> Path:
    return report_path.with_suffix(report_path.suffix + ".manifest.json")


def _report_path(output_format: ReportFormat) -> Path:
    return REPORT_DIR / f"hermes-report.{output_format}"


def _validation_result(errors: list[str]) -> dict[str, Any]:
    return _sanitize_mcp_result({
        "completed": False,
        "exit_code": None,
        "status": "validation_error",
        "validation_errors": errors,
        "report_path": None,
        "manifest_path": None,
        "summary": "NISA Quant refresh was not started because the arguments are invalid.",
    })


def _validate_refresh_arguments(
    *,
    as_of: str,
    start: str,
    end: str,
    mode: str,
    limit: int | None,
    sec_contact: str | None,
    output_format: str,
) -> list[str]:
    errors: list[str] = []
    parsed_dates: dict[str, date] = {}
    for name, value in (("as_of", as_of), ("start", start), ("end", end)):
        if not isinstance(value, str) or not _DATE_PATTERN.fullmatch(value):
            errors.append(f"{name} must be a YYYY-MM-DD string")
            continue
        try:
            parsed_dates[name] = date.fromisoformat(value)
        except ValueError:
            errors.append(f"{name} is not a valid calendar date")
    if "start" in parsed_dates and "end" in parsed_dates and parsed_dates["start"] > parsed_dates["end"]:
        errors.append("start must be on or before end")
    if mode not in {"live", "replay"}:
        errors.append("mode must be live or replay")
    if output_format not in {"markdown", "json"}:
        errors.append("format must be markdown or json")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIVE_LIMIT:
            errors.append(f"limit must be a positive integer no greater than {MAX_LIVE_LIMIT}")
        if mode == "replay":
            errors.append("limit is only valid in live mode")
    if sec_contact is not None:
        if not isinstance(sec_contact, str) or not sec_contact.strip():
            errors.append("sec_contact must be a non-empty non-secret string")
        elif len(sec_contact) > 256 or any(ord(char) < 32 for char in sec_contact) or _SECRET_PATTERN.search(sec_contact):
            errors.append("sec_contact must be a short non-secret contact string")
        if mode == "replay":
            errors.append("sec_contact is only valid in live mode")
    return errors


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def _bounded_list(value: Any, *, limit: int = MAX_FAILURES) -> list[Any]:
    return value[:limit] if isinstance(value, list) else []


def _bounded_mapping(value: Any, *, limit: int = MAX_GAPS) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in list(value.items())[:limit]}


def _sanitize_output_text(value: str, *, limit: int) -> str:
    """Remove local-path/secret material and keep one string bounded."""
    sanitized = _LOCAL_PATH_WITH_SPACES_PATTERN.sub(_replace_local_path, value)
    sanitized = _LOCAL_PATH_PATTERN.sub(_replace_local_path, sanitized)
    sanitized = _SECRET_BLOCK_ASSIGNMENT_PATTERN.sub(
        lambda match: match.group("prefix") + _SECRET_MARKER, sanitized
    )
    sanitized = _SECRET_QUOTED_ASSIGNMENT_PATTERNS[0].sub(
        lambda match: _replace_quoted_secret_assignment(match, '"'), sanitized
    )
    sanitized = _SECRET_QUOTED_ASSIGNMENT_PATTERNS[1].sub(
        lambda match: _replace_quoted_secret_assignment(match, "'"), sanitized
    )
    sanitized = _SECRET_QUOTED_ASSIGNMENT_PATTERNS[2].sub(
        lambda match: _replace_quoted_secret_assignment(match, '"'), sanitized
    )
    sanitized = _SECRET_QUOTED_ASSIGNMENT_PATTERNS[3].sub(
        lambda match: _replace_quoted_secret_assignment(match, "'"), sanitized
    )
    sanitized = _SECRET_COLLECTION_ASSIGNMENT_PATTERN.sub(
        lambda match: match.group("prefix") + _SECRET_MARKER, sanitized
    )
    sanitized = _SECRET_YAML_ASSIGNMENT_PATTERN.sub(
        lambda match: match.group("prefix") + _SECRET_MARKER, sanitized
    )
    sanitized = _AUTHORIZATION_BEARER_PATTERN.sub(
        lambda match: match.group("prefix") + _SECRET_MARKER,
        sanitized,
    )
    sanitized = _SECRET_SHELL_ASSIGNMENT_PATTERN.sub(
        lambda match: match.group("prefix") + _SECRET_MARKER,
        sanitized,
    )
    sanitized = _SECRET_UNQUOTED_ASSIGNMENT_PATTERN.sub(
        lambda match: match.group("prefix") + _SECRET_MARKER,
        sanitized,
    )
    if len(sanitized) <= limit:
        return sanitized
    if limit <= len(_OUTPUT_TRUNCATION_MARKER):
        return sanitized[:limit]
    return sanitized[: limit - len(_OUTPUT_TRUNCATION_MARKER)] + _OUTPUT_TRUNCATION_MARKER


def _sanitize_mcp_value(value: Any, *, depth: int = 0, key: str | None = None) -> Any:
    """Recursively bound values crossing the Hermes MCP output boundary."""
    if key is not None and _SECRET_PATTERN.search(key):
        return _SECRET_MARKER
    if isinstance(value, str):
        limit = MAX_REPORT_CONTENT if key == "report_content" else MAX_OUTPUT_STRING
        return _sanitize_output_text(value, limit=limit)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= MAX_OUTPUT_DEPTH:
        return _OUTPUT_DEPTH_MARKER
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:MAX_OUTPUT_COLLECTION_ITEMS]:
            safe_key = _sanitize_output_text(str(raw_key), limit=MAX_OUTPUT_STRING)
            sanitized[safe_key] = _sanitize_mcp_value(item, depth=depth + 1, key=str(raw_key))
        return sanitized
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_mcp_value(item, depth=depth + 1)
            for item in value[:MAX_OUTPUT_COLLECTION_ITEMS]
        ]
    return _sanitize_output_text(str(value), limit=MAX_OUTPUT_STRING)


def _sanitize_mcp_result(result: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_mcp_value(result)
    assert isinstance(sanitized, dict)
    return sanitized


def _predictions_from_artifacts(report: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    predictions = report.get("current_predictions")
    if not isinstance(predictions, list):
        metadata = manifest.get("artifact_metadata")
        predictions = metadata.get("current_predictions") if isinstance(metadata, dict) else []
    return [item for item in predictions if isinstance(item, dict)]


def _freshness_from_artifacts(report: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    freshness = manifest.get("current_prediction_freshness")
    if not isinstance(freshness, list):
        metadata = manifest.get("artifact_metadata")
        freshness = metadata.get("current_prediction_freshness") if isinstance(metadata, dict) else []
    if not isinstance(freshness, list):
        freshness = [item.get("freshness_evidence") for item in _predictions_from_artifacts(report, manifest)]
    return [item for item in freshness if isinstance(item, dict)]


def _summary_from_artifacts(
    *,
    report: dict[str, Any],
    manifest: dict[str, Any],
    exit_code: int,
    report_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    predictions = _predictions_from_artifacts(report, manifest)
    tickers = [ticker for ticker in (item.get("ticker") for item in predictions) if isinstance(ticker, str)]
    freshness = _freshness_from_artifacts(report, manifest)
    freshness_counts = dict(sorted(Counter(str(item.get("status", "unknown")) for item in freshness).items()))
    metadata = manifest.get("artifact_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    status = report.get("status") or manifest.get("report_status") or "unavailable"
    suppressed = report.get("performance_claims_suppressed")
    if not isinstance(suppressed, bool):
        suppressed = metadata.get("performance_claims_suppressed") is True
    failures = _bounded_list(manifest.get("failures"))
    gaps = _bounded_mapping(manifest.get("gaps"))
    failure_count = len(manifest.get("failures", [])) if isinstance(manifest.get("failures"), list) else 0
    gap_count = len(manifest.get("gaps", {})) if isinstance(manifest.get("gaps"), dict) else 0
    ticker_text = ", ".join(tickers) if tickers else "none"
    summary = (
        f"NISA Quant refresh {status} (exit {exit_code}); "
        f"{len(tickers)} current prediction(s): {ticker_text}; "
        f"performance claims suppressed={str(suppressed).lower()}; "
        f"SEC={manifest.get('sec_status', 'unknown')}."
    )
    if failure_count or gap_count:
        summary += f" Evidence includes {failure_count} failure(s) and {gap_count} gap record(s)."
    return _sanitize_mcp_result({
        "completed": True,
        "exit_code": exit_code,
        "status": status,
        "report_path": _relative_path(report_path),
        "manifest_path": _relative_path(manifest_path),
        "current_prediction_count": len(tickers),
        "current_prediction_tickers": tickers,
        "freshness_summary": freshness_counts,
        "freshness_evidence": freshness[:MAX_FAILURES],
        "sec_status": manifest.get("sec_status", "unknown"),
        "performance_claims_suppressed": suppressed,
        "gaps": gaps,
        "failures": failures,
        "failures_by_ticker": _bounded_mapping(manifest.get("failures_by_ticker")),
        "summary": summary,
    })


def _load_refresh_artifacts(report_path: Path, manifest_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    report = _read_json_object(report_path) if report_path.suffix == ".json" else {}
    manifest = _read_json_object(manifest_path)
    return report, manifest


def _latest_report_path() -> Path | None:
    existing = [path for path in LATEST_REPORT_PATHS if path.is_file()]
    if not existing:
        return None
    complete_pairs: list[tuple[str, float, Path]] = []
    for report_path in existing:
        manifest_path = _manifest_path(report_path)
        if not manifest_path.is_file():
            continue
        try:
            manifest = _read_json_object(manifest_path)
            retrieved_at = manifest.get("retrieved_at")
            stamp = retrieved_at if isinstance(retrieved_at, str) else ""
            mtime = report_path.stat().st_mtime
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        complete_pairs.append((stamp, mtime, report_path))
    if complete_pairs:
        return max(complete_pairs, key=lambda item: (item[0], item[1]))[2]
    return max(existing, key=lambda path: path.stat().st_mtime)


def nisa_quant_refresh(
    as_of: str,
    start: str,
    end: str,
    mode: Mode = "replay",
    limit: int | None = None,
    sec_contact: str | None = None,
    format: ReportFormat = "markdown",
) -> dict[str, Any]:
    """Refresh the fixed local Phase 3 report through replay or explicit live mode."""
    errors = _validate_refresh_arguments(
        as_of=as_of, start=start, end=end, mode=mode, limit=limit,
        sec_contact=sec_contact, output_format=format,
    )
    if errors:
        return _validation_result(errors)

    report_path = _report_path(format)
    manifest_path = _manifest_path(report_path)
    try:
        exit_code = refresh_phase3(
            as_of=as_of,
            start=start,
            end=end,
            cache_dir=PHASE3_CACHE_DIR,
            output=report_path,
            live=mode == "live",
            replay_only=mode == "replay",
            limit=limit,
            sec_contact=sec_contact if mode == "live" else None,
        )
        report, manifest = _load_refresh_artifacts(report_path, manifest_path)
    except (OSError, TypeError, ValueError, OverflowError, ZeroDivisionError) as exc:
        return _sanitize_mcp_result({
            "completed": False,
            "exit_code": 2,
            "status": "producer_error",
            "report_path": _relative_path(report_path),
            "manifest_path": _relative_path(manifest_path),
            "failures": [f"{type(exc).__name__}: {exc}"],
            "summary": "NISA Quant refresh could not produce a structured report.",
        })
    return _summary_from_artifacts(
        report=report, manifest=manifest, exit_code=int(exit_code),
        report_path=report_path, manifest_path=manifest_path,
    )


def nisa_quant_latest() -> dict[str, Any]:
    """Read only the fixed latest Hermes report and matching manifest."""
    report_path = _latest_report_path()
    if report_path is None:
        return _sanitize_mcp_result({
            "completed": True,
            "exit_code": None,
            "status": "not_yet_run",
            "report_path": None,
            "manifest_path": None,
            "report_content": None,
            "summary": "NISA Quant Hermes refresh has not run yet.",
        })
    manifest_path = _manifest_path(report_path)
    try:
        report, manifest = _load_refresh_artifacts(report_path, manifest_path)
        content = report_path.read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _sanitize_mcp_result({
            "completed": True,
            "exit_code": 2,
            "status": "incomplete_latest",
            "report_path": _relative_path(report_path),
            "manifest_path": _relative_path(manifest_path),
            "report_content": None,
            "failures": [f"{type(exc).__name__}: {exc}"],
            "summary": "The latest NISA Quant Hermes report is incomplete or unreadable.",
        })
    result = _summary_from_artifacts(
        report=report, manifest=manifest,
        exit_code=0 if manifest.get("report_status") in {"available", "available_descriptive"} else 2,
        report_path=report_path,
        manifest_path=manifest_path,
    )
    result["report_content"] = content
    result["report_content_truncated"] = len(content) > MAX_REPORT_CONTENT
    return _sanitize_mcp_result(result)


def build_mcp_server() -> Any:
    """Build the Hermes-venv MCP server without importing its SDK for NISA callers."""
    from typing import Annotated

    from mcp.server.mcpserver import MCPServer
    from pydantic import Field

    DateArgument = Annotated[str, Field(pattern=_DATE_PATTERN.pattern)]
    LimitArgument = Annotated[int | None, Field(ge=1, le=MAX_LIVE_LIMIT)]
    ContactArgument = Annotated[str | None, Field(max_length=256)]

    def refresh_tool(
        as_of: DateArgument,
        start: DateArgument,
        end: DateArgument,
        mode: Mode = "replay",
        limit: LimitArgument = None,
        sec_contact: ContactArgument = None,
        format: ReportFormat = "markdown",
    ) -> dict[str, Any]:
        return nisa_quant_refresh(
            as_of=as_of,
            start=start,
            end=end,
            mode=mode,
            limit=limit,
            sec_contact=sec_contact,
            format=format,
        )

    # ``from __future__ import annotations`` stores nested aliases as strings,
    # while MCPServer evaluates annotations in module globals. Bind the local
    # runtime-only Pydantic aliases explicitly before registration.
    refresh_tool.__annotations__ = {
        "as_of": DateArgument,
        "start": DateArgument,
        "end": DateArgument,
        "mode": Mode,
        "limit": LimitArgument,
        "sec_contact": ContactArgument,
        "format": ReportFormat,
        "return": dict[str, Any],
    }

    mcp_server = MCPServer(
        name="nisa-quant-local",
        version="0.1.0",
        description="Safe local Phase 3 NISA quant replay/live-refresh tools.",
    )
    mcp_server.add_tool(
        refresh_tool,
        name="nisa_quant_refresh",
        description=(
            "Refresh the existing Phase 3 producer using fixed repo-local paths. "
            "Replay is the default and only live mode may use public read-only network sources."
        ),
        structured_output=True,
    )
    mcp_server.add_tool(
        nisa_quant_latest,
        name="nisa_quant_latest",
        description="Read the fixed latest Hermes NISA Quant report and bounded report content.",
        structured_output=True,
    )
    return mcp_server


def main() -> None:
    """Run the local MCP server over stdio."""
    try:
        build_mcp_server().run("stdio")
    except Exception as exc:
        print(f"nisa_quant_mcp_server: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
