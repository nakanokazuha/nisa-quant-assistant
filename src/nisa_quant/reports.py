"""Cited Markdown rendering from a structured, deterministic snapshot."""

from __future__ import annotations

import json
import re
from typing import Any

from .screens import LABELS


REQUIRED_HEADINGS = (
    "# NISA Quant Assistant Report",
    "## Data cutoffs",
    "## Data warnings",
    "## Ranked candidates",
    "## Source list",
)


def _number(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def render_report(
    snapshot: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    provider: str,
    template_version: str,
) -> str:
    """Render only values present in the supplied snapshot/candidate objects."""
    lines = [
        "# NISA Quant Assistant Report",
        "",
        f"Report generated from local snapshot as of `{snapshot['as_of']}`.",
        f"Provider/model identifier: `{provider}`; template: `{template_version}`.",
        "",
        "## Data cutoffs",
        "",
    ]
    for name, cutoff in snapshot["data_cutoffs"].items():
        lines.append(f"- {name}: `{cutoff or 'unavailable'}`")
    lines.extend(["", "## Portfolio snapshot", "", f"- Market value: `{_number(snapshot['portfolio']['market_value'])}`", f"- Cost basis: `{_number(snapshot['portfolio']['cost_basis'])}`", f"- Contributions: `{_number(snapshot['portfolio']['contributions'])}`", f"- Distributions: `{_number(snapshot['portfolio']['distributions'])}`", ""])
    lines.extend(["## Data warnings", ""])
    if snapshot["warnings"]:
        lines.extend(f"- `{warning['code']}`: {warning['message']}" for warning in snapshot["warnings"])
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Ranked candidates", "", "| Instrument | Account | Label | Evidence | Metrics | Sources |", "|---|---|---|---|---|---|"])
    for candidate in candidates:
        metrics = ", ".join(
            f"{key}={_number(value)}" for key, value in candidate["metrics"].items()
        )
        source_ids = ", ".join(f"[{source_id}]" for source_id in candidate["source_ids"]) or "unavailable"
        lines.append(
            f"| {candidate['instrument']} | {candidate['account']} | {candidate['label']} | {candidate['evidence_quality']} | {metrics} | {source_ids} |"
        )
        lines.extend(
            [
                "",
                f"**{candidate['instrument']} — {candidate['label']}**",
                f"- Reason: {candidate['reason']}",
                f"- Risk/counter-evidence: {candidate['risk_counter_evidence']}",
                f"- Horizon: {candidate['horizon']}",
                f"- Invalidation/what changes the view: {candidate['invalidation']}",
                f"- {candidate['manual_review']}",
            ]
        )
    lines.extend(["", "## Source list", ""])
    for source in snapshot["sources"]:
        lines.append(
            f"- [{source['id']}] — {source['source_name']} ({source['source_url_or_identifier']}), retrieved `{source['retrieved_at']}`, observed `{source['observation_date'] or 'unavailable'}`, location `{source['citation_location']}`"
        )
    lines.extend(["", "Manual review required; no order was placed."])
    report = "\n".join(lines) + "\n"
    validate_report(report)
    return report


def validate_report(report: str) -> None:
    """Reject reports that omit safety structure or use unsupported labels."""
    for heading in REQUIRED_HEADINGS:
        if heading not in report:
            raise ValueError(f"report is missing required section: {heading}")
    if "Manual review required; no order was placed." not in report:
        raise ValueError("report is missing the no-order statement")
    labels = set(re.findall(r"\| (BUY CANDIDATE|HOLD|SELL / REDUCE CANDIDATE|WATCH|NO ACTION / INSUFFICIENT DATA) \|", report))
    if not labels <= LABELS:
        raise ValueError("report contains an unsupported recommendation label")
    if "api_key" in report.lower() or "account_number" in report.lower():
        raise ValueError("report contains a prohibited secret/account field")
    if not re.search(r"\[?SRC-[a-f0-9]{12}\]?", report):
        raise ValueError("report has no source citations")
