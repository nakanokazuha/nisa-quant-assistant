"""Cited Markdown rendering with strict provenance and redaction checks.

Structured validation requires the caller to bind the expected provider/model
identifier and template version; those values are part of the report contract.
Safety scanning recognizes canonical labels without allowing them to mask
action-like imperatives, scans supplied structured values before binding, and
rejects account-identifier-shaped content.  A bound renderer report must equal
the renderer's exact output byte-for-byte; the only normalization exception is
format-character removal inside the three fixed marker labels, never inside
their values or any other canonical content.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from numbers import Real
from typing import Any, Iterable

from .screens import LABELS, _candidate_from_record


REQUIRED_HEADINGS = (
    "# NISA Quant Assistant Report",
    "## Data cutoffs",
    "## Data warnings",
    "## Ranked candidates",
    "## Source list",
)
SOURCE_ID_PATTERN = r"SRC-[a-f0-9]{12}"
UNSUPPORTED_ACTION_PROSE = (
    re.compile(r"\b(?:PURCHASE|BUY|SELL|ACCUMULATE)\s+NOW\b", re.IGNORECASE),
    re.compile(r"\bSTRONG\s+(?:BUY|SELL)\b", re.IGNORECASE),
    re.compile(r"\bGUARANTEED\s+RETURN\b", re.IGNORECASE),
    re.compile(r"\b(?:BUY|SELL)\s+(?:SIGNAL|RECOMMENDATION|ALERT)\b", re.IGNORECASE),
    re.compile(r"\b(?:MAYBE|UNKNOWN|UNSUPPORTED)\s+(?:ACTION|SIGNAL|RECOMMENDATION)\b", re.IGNORECASE),
    re.compile(r"\b(?:PLEASE|MUST|SHOULD|RECOMMEND(?:S|ED)?|CONSIDER)\s+(?:TO\s+)?(?:PURCHASE|BUY|SELL|ACCUMULATE)\b", re.IGNORECASE),
    re.compile(r"\b(?:DO\s+NOT|DON'T|NEVER|AVOID)\s+(?:PURCHASE|BUY|SELL|ACCUMULATE)\b", re.IGNORECASE),
    re.compile(r"\b(?:PURCHASE|BUY|SELL|ACCUMULATE)\b", re.IGNORECASE),
)
ACTION_LIKE_IMPERATIVE = re.compile(
    r"\b(?:PURCHASE|BUY|SELL|ACCUMULATE)\b[^\n|]{0,80}?\b(?:NOW|IMMEDIATELY)\b",
    re.IGNORECASE,
)
PII_FILENAME_PATTERNS = (
    r"(?<![\w-])(?-i:[A-Z][a-z]{1,}[-_][A-Z][a-z]{1,})\.(?:csv|json|sqlite|pdf|xlsx?)\b",
    r"(?<![\w-])[\u3400-\u9fff]{3,}\.(?:csv|json|sqlite|pdf|xlsx?)\b",
    r"\b[a-z]+-(?:portfolio|account|statement|summary)\.(?:csv|json|sqlite|pdf|xlsx?)\b",
    r"\b(?:taro|hanako|john|jane|alice|bob|charlie|david|emma|michael|sarah|ilham)-[a-z]+(?:-portfolio)?\.(?:csv|json|sqlite|pdf|xlsx?)\b",
    r"\b[a-z]+-(?:yamada|tanaka|sato|suzuki|watanabe|takahashi|ito|doe|smith|garcia|chen|wang|lee)(?:-portfolio)?\.(?:csv|json|sqlite|pdf|xlsx?)\b",
    r"\b(?:taro|hanako|john|jane|alice|bob|charlie|david|emma|michael|sarah|ilham)[_-][a-z]+\.(?:csv|json|sqlite|pdf|xlsx?)\b",
    r"\b(?:taro|hanako|john|jane|alice|bob|charlie|david|emma|michael|sarah|ilham|kevin)[._-](?:holdings|portfolio|account|statement|summary)\.(?:csv|json|sqlite|pdf|xlsx?)\b",
    r"\b[a-z]+[._-](?:holdings|portfolio|account|statement|summary)\.(?:csv|json|sqlite|pdf|xlsx?)\b",
    r"(?<![\w-])(?:山田|田中|佐藤|鈴木|高橋|伊藤|渡辺|加藤|吉田|山本|中村|小林|斎藤|井上|木村|林|清水)[\u3040-\u30ff\u3400-\u9fff]{0,3}\.(?:csv|json|sqlite|pdf|xlsx?)\b",
    r"(?<![\w-])(?:[A-Za-z]{2,}(?:[\s_]+[A-Za-z]{2,})+)\.(?:csv|json|sqlite|pdf|xlsx?)\b",
    r"(?<![\w-])[\u3400-\u9fff]{2,}\.(?:csv|json|sqlite|pdf|xlsx?)\b",
    r"(?<![\w-])[\uac00-\ud7af]{2,}\.(?:csv|json|sqlite|pdf|xlsx?)\b",
)
ACTION_WORD_PATTERN = re.compile(
    r"\b(?:HOLD|WATCH|REDUCE|BUY|SELL|PURCHASE|ACCUMULATE|ORDER|EXECUTE)\b",
    re.IGNORECASE,
)
ACTION_GERUND_PATTERN = re.compile(
    r"\b(?:BUYING|SELLING|HOLDING|WATCHING|REDUCING|PURCHASING|ACCUMULATING|"
    r"ORDERING|EXECUTING)\b",
    re.IGNORECASE,
)
ALLOWED_NON_ACTION_GERUNDS = (
    "largest accepted holding market-value weight",
    "constant current-value weights over common dated holding observations",
    "required holding lacks a usable value",
    "largest_holding_pct",
)
NONFINITE_PATTERN = re.compile(r"(?<![A-Za-z])[-+]?(?:nan|inf(?:inity)?)(?![A-Za-z])", re.IGNORECASE)
CONFUSABLE_TRANSLATION = str.maketrans({
    "Α": "A", "α": "a", "Β": "B", "β": "b", "Γ": "G", "γ": "g",
    "Ε": "E", "ε": "e", "Ζ": "Z", "ζ": "z", "Η": "H", "η": "h",
    "Ι": "I", "ι": "i", "Κ": "K", "κ": "k", "Μ": "M", "μ": "m",
    "Ν": "N", "ν": "n", "Ο": "O", "ο": "o", "Ρ": "P", "ρ": "p",
    "Τ": "T", "τ": "t", "Υ": "Y", "υ": "y", "Χ": "X", "χ": "x",
    "ϵ": "e", "С": "C", "с": "c", "Е": "E", "е": "e", "О": "O", "о": "o",
    "Р": "P", "р": "p", "Х": "X", "х": "x", "А": "A", "а": "a",
    "Т": "T", "т": "t", "К": "K", "к": "k",
    "В": "B", "в": "b", "У": "Y", "у": "y", "М": "M", "м": "m",
    "Н": "H", "н": "h",
})
SAFE_REPORT_FILENAMES = frozenset({
    "prices.csv", "価格.csv", "price-history.csv", "synthetic_broker.csv",
    "synthetic_prices.csv", "synthetic_distributions.csv",
})


def _marker_pattern(words: str) -> re.Pattern[str]:
    letters = "".join(
        character for character in words if character.isascii() and character.isalpha()
    )
    separator = r"[\W_]*"
    return re.compile(
        rf"(?<![A-Za-z]){separator.join(re.escape(letter) for letter in letters)}(?![A-Za-z])",
        re.IGNORECASE,
    )


RENDERER_MARKER_PATTERNS = (
    _marker_pattern("Report generated"),
    _marker_pattern("Provider model identifier"),
    _marker_pattern("Report contract fingerprint"),
)
RENDERER_GENERATED_LINE = re.compile(
    r"Report generated from local snapshot as of `[^`\r\n]+`\."
)
RENDERER_METADATA_LINE = re.compile(
    r"Provider/model identifier: `([^`\r\n]+)`; template: `([^`\r\n]+)`\."
)
RENDERER_FINGERPRINT_LINE = re.compile(
    r"Report contract fingerprint: `[0-9a-f]{64}`\."
)
PORTFOLIO_PROVENANCE_FIELDS = (
    "market_value", "cost_basis", "cost_basis_by_currency", "contributions", "distributions",
    "realized_pl_by_currency", "allocation", "concentration", "drawdown", "volatility",
)


def _number(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _candidate_source_ids(candidate: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(candidate.get("source_ids", []) + candidate.get("ledger_source_ids", [])))


def _metrics_text(candidate: dict[str, Any]) -> str:
    return ", ".join(f"{key}={_number(value)}" for key, value in candidate["metrics"].items())


def _citations_text(source_ids: list[str]) -> str:
    return ", ".join(f"[{source_id}]" for source_id in source_ids) or "unavailable"


def _mapping_text(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def _normalize_action_tokens(value: str) -> str:
    """Collapse punctuation/spacing inside action words for safety scanning."""
    for word in (
        "ACCUMULATING", "ACCUMULATED", "ACCUMULATE", "PURCHASING", "PURCHASED", "PURCHASE",
        "REDUCING", "REDUCED", "REDUCE", "SELLING", "SELLED", "SELL", "BUYING", "BUYS", "BUY",
        "HOLDING", "HOLDS", "HOLD", "WATCHING", "WATCHES", "WATCH", "ORDERING", "ORDERS", "ORDER",
        "EXECUTING", "EXECUTED", "EXECUTE", "BOUGHT", "SOLD", "HELD", "SELLS",
    ):
        letters = r"[\W_]*".join(re.escape(letter) for letter in word)
        value = re.sub(
            rf"(?<![A-Za-z]){letters}(?![A-Za-z])",
            word,
            value,
            flags=re.IGNORECASE,
        )
    for word, canonical in (
        ("PURCHASED", "PURCHASE"), ("ACCUMULATED", "ACCUMULATE"),
        ("BOUGHT", "BUY"), ("SOLD", "SELL"), ("HELD", "HOLD"),
        ("WATCHES", "WATCH"), ("SELLS", "SELL"),
    ):
        value = re.sub(rf"\b{word}\b", canonical, value, flags=re.IGNORECASE)
    return value


def _normalize_identifier_tokens(value: str) -> str:
    """Collapse punctuation/spacing inside disclosure and secret keywords."""
    for word in (
        "account", "broker", "customer", "portfolio", "identifier", "id", "no", "ref",
        "password", "passwd", "authorization", "basic", "bearer", "secret", "private",
        "api", "access", "token", "key", "aws_secret_access_key",
        "sk",
    ):
        letters = r"[\W_]*".join(re.escape(letter) for letter in word)
        value = re.sub(
            rf"(?<![A-Za-z]){letters}(?![A-Za-z])",
            word,
            value,
            flags=re.IGNORECASE,
        )
    return value


def _normalize_safety_text(value: str) -> str:
    """Normalize compatibility forms and remove invisible format characters."""
    return "".join(
        character for character in unicodedata.normalize("NFKC", value)
        if unicodedata.category(character) != "Cf"
    ).translate(CONFUSABLE_TRANSLATION)


def _normalize_security_text(value: str) -> str:
    """Normalize dash punctuation for separator-obfuscated credentials."""
    return "".join(
        "-" if unicodedata.category(character) == "Pd" else character
        for character in value
    )


def _mask_safe_filenames(value: str) -> str:
    for filename in SAFE_REPORT_FILENAMES:
        # Use punctuation-only replacement so the mask cannot become an
        # account-like token when it follows a metadata word such as broker.
        value = re.sub(
            rf"(?<![A-Za-z0-9_-]){re.escape(filename)}(?![A-Za-z0-9_-])",
            "__",
            value,
        )
    return value


def _mask_allowed_non_action_gerunds(value: str) -> str:
    for phrase in ALLOWED_NON_ACTION_GERUNDS:
        value = re.sub(re.escape(phrase), "__CANONICAL_SAFE_EXPLANATION__", value, flags=re.IGNORECASE)
    return value


def _mask_structural_labels(report: str) -> str:
    """Mask only exact renderer-owned label positions for the safety scan."""
    masked_lines: list[str] = []
    in_ranked_section = False
    for line in report.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == "## Ranked candidates":
            in_ranked_section = True
        elif in_ranked_section and stripped.startswith("## "):
            in_ranked_section = False
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        if in_ranked_section and content.lstrip().startswith("|"):
            cells = content.strip().strip("|").split("|")
            if len(cells) >= 6 and cells[2].strip() in LABELS:
                cells[2] = " __CANONICAL_LABEL__ "
                content = "|".join(cells)
        if in_ranked_section:
            content = re.sub(
            r"(\*\*[^\n|]*?\s[—-]\s*)([^*]+)(\*\*)",
            lambda match: (
                match.group(1) + "__CANONICAL_LABEL__" + match.group(3)
                if match.group(2).strip() in LABELS else match.group(0)
            ),
            content,
            )
        masked_lines.append(content + ending)
    return "".join(masked_lines)


def _validate_report_safety(report: str) -> None:
    """Reject action instructions, secrets, account identifiers, and PII filenames."""
    normalized = _normalize_security_text(_normalize_identifier_tokens(_normalize_safety_text(report)))
    normalized = _mask_safe_filenames(normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"[ \t]*/[ \t]*", "/", normalized)
    secret_patterns = (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        r"\b(?:ghp|gho|github_pat|sk|xoxb)[_-][A-Za-z0-9_-]{16,}\b",
        r"\b(?:github_pat_|xoxb-|glpat-|rk_live_|gho_|AIza)",
        r"\bAIza[A-Za-z0-9_-]{20,}\b",
        r"\bAuthorization\s*:?\s*(?:Basic|Bearer)\s+\S+",
        r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
        r"\b(?:api\s*key|access\s*token|bearer|secret|private\s*key)\s*[:=]\s*\S+",
        r"\b(?:token|auth\s*token)\s*[:=]\s*[A-Za-z0-9_-]{20,}",
        r"\b(?:password|passwd)\s*(?::|=)?\s*\S+",
        r"\bAWS_SECRET_ACCESS_KEY\s*(?::|=)?\s*\S+",
        r"\b(?:bearer|basic)\s+[A-Za-z0-9][A-Za-z0-9._~+/=-]{19,}\b",
        r"\b(?:api[\W_]*key|access[\W_]*token|secret|private[\W_]*key)\s+[A-Za-z0-9][A-Za-z0-9._~+/=-]{19,}\b",
        r"\b(?:ya29\.)[A-Za-z0-9_-]{20,}\b",
        r"\bAccountKey\s*=\s*[A-Za-z0-9+/=]{20,}\b",
        r"(?<![A-Za-z])(?:account|broker|customer|portfolio)[ \t]+[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*\b",
        r"(?<![A-Za-z])(?:account|broker|customer|portfolio)[ \t]+(?-i:[A-Z][A-Z0-9_-]{2,})\b",
        r"(?<![A-Za-z])(?:account|broker|customer|portfolio)(?:(?:[ \t_./-]+(?:account|identifier|number|id|no|ref))[._\-/]*)?[ \t]*(?::|=|#)[ \t]*(?:is[ \t]+)?[^\s,;|`]+",
        r"(?<![A-Za-z])(?:account|broker|customer|portfolio)[ \t_./-]+(?:account|identifier|number|id|no|ref)[._\-/]*[ \t]+(?:is[ \t]+)?[^\s,;|`]+",
        r"(?<![A-Za-z])(?:account|broker|customer|portfolio)(?:[\W_]+(?:is[\W_]+)?)?(?:(?-i:[A-Z][A-Z0-9_-]{2,})|[A-Za-z]+[0-9][A-Za-z0-9_-]*)\b",
        r"(?:口座|顧客)(?:番号|id)?\s*(?::|=|#)?\s*[^\s,;|`]+",
        r"(?:^|[/\\])[^\s/\\]*\d{6,}[^\s/\\]*\.(?:csv|json|sqlite)\b",
        r"(?<![\w-])[\w-]*[^\x00-\x7f][\w-]*-(?:holdings|portfolio|account|statement|ledger|summary)\.(?:csv|json|sqlite|pdf|xlsx?)\b",
        *PII_FILENAME_PATTERNS,
    )
    if any(re.search(pattern, normalized, re.IGNORECASE | re.MULTILINE) for pattern in secret_patterns):
        raise ValueError("report contains a prohibited secret or account identifier")
    if NONFINITE_PATTERN.search(normalized):
        raise ValueError("report contains a non-finite numeric value")

    masked_report = _mask_structural_labels(normalized).replace(
        "Manual review required; no order was placed.", "__CANONICAL_SAFE_STATEMENT__",
    ).replace(
        "Manual review required; no order can be placed by this tool.", "__CANONICAL_SAFE_STATEMENT__",
    )
    masked_report = _mask_allowed_non_action_gerunds(masked_report)
    safety_text = _normalize_action_tokens(re.sub(r"[._\-/]+", " ", masked_report))
    if ACTION_WORD_PATTERN.search(safety_text) or ACTION_GERUND_PATTERN.search(safety_text):
        raise ValueError("report contains unsupported action-like recommendation prose")

    # Inspect action phrases before masking canonical labels.  Only a label in
    # its renderer-owned table cell or bold heading is allowed to be a label;
    # the same words in explanatory prose remain an unsafe imperative.
    action_candidate = re.compile(
        r"\b(?:PURCHASE|BUY|SELL|ACCUMULATE|REDUCE|HOLD|WATCH)(?:\s+(?:PURCHASE|BUY|SELL|ACCUMULATE|REDUCE|HOLD|WATCH))*\s+CANDIDATE\b",
        re.IGNORECASE,
    )
    allowed_action_labels = {
        re.sub(r"\s+", " ", label.replace("/", " ")).strip().upper()
        for label in LABELS
    }
    for line in normalized.splitlines():
        action_line = _normalize_action_tokens(re.sub(r"[._\-/]+", " ", line))
        for match in action_candidate.finditer(action_line):
            phrase = re.sub(r"\s+", " ", match.group(0)).strip().upper()
            cells = [
                re.sub(r"\s+", " ", cell.replace("/", " ")).strip().upper()
                for cell in line.strip().strip("|").split("|")
            ]
            canonical_table_label = len(cells) >= 3 and cells[2] == phrase
            heading_match = re.search(r"[—-]\s*(.*?)\s*\*\*\s*$", line)
            canonical_heading_label = bool(
                heading_match
                and re.sub(r"\s+", " ", heading_match.group(1).replace("/", " ")).strip().upper() == phrase
            )
            if phrase not in allowed_action_labels:
                raise ValueError("report contains unsupported action-like recommendation prose")
            if not (canonical_table_label or canonical_heading_label):
                raise ValueError("report contains unsupported action-like recommendation prose")

    # Detect other imperatives while canonical labels are still present.  The
    # explicit action scan above prevents label deletion from masking variants.
    normalized_action_text = _normalize_action_tokens(re.sub(r"[._\-/]+", " ", normalized))
    normalized_action_text = re.sub(r"\s+", " ", normalized_action_text)
    imperative_patterns = (
        r"\b(?:PURCHASE|BUY|SELL|ACCUMULATE|REDUCE|HOLD|WATCH)(?:\s+(?:PURCHASE|BUY|SELL|ACCUMULATE|REDUCE|HOLD|WATCH|CANDIDATE))*\s+(?:NOW|IMMEDIATELY|TODAY|TOMORROW)\b",
        r"\b(?:PLEASE|MUST|SHOULD|RECOMMEND(?:S|ED)?|CONSIDER)\s+(?:TO\s+)?(?:PURCHASE|BUY|SELL|ACCUMULATE|REDUCE|HOLD|WATCH)\b",
        r"\b(?:HOLD|WATCH)\s+(?:SHARES?|EARNINGS?)\b",
        r"\b(?:REDUCE|SELL)\s+(?:EXPOSURE|POSITION|HOLDINGS?|RISK)\b",
    )
    if ACTION_LIKE_IMPERATIVE.search(normalized_action_text) or any(
        re.search(pattern, normalized_action_text, re.IGNORECASE) for pattern in imperative_patterns
    ):
        raise ValueError("report contains unsupported action-like recommendation prose")

    for pattern in UNSUPPORTED_ACTION_PROSE:
        for match in pattern.finditer(normalized):
            line_start = normalized.rfind("\n", 0, match.start()) + 1
            line_end = normalized.find("\n", match.end())
            line = normalized[line_start:] if line_end == -1 else normalized[line_start:line_end]
            stripped_line = line.strip()
            cells = [cell.strip() for cell in stripped_line.strip("|").split("|")]
            canonical_table_label = len(cells) >= 3 and cells[2].upper() in LABELS
            heading_match = re.fullmatch(r"\*\*[^\n|]*\s[—-]\s([^*]+)\*\*", stripped_line)
            canonical_heading_label = bool(
                heading_match and heading_match.group(1).strip().upper() in LABELS
            )
            if not (canonical_table_label or canonical_heading_label):
                raise ValueError("report contains unsupported action-like recommendation prose")


def _structured_texts(value: Any, *, allow_renderer_labels: bool = False) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            if allow_renderer_labels and key == "label" and isinstance(nested, str) and nested in LABELS:
                continue
            if key == "field" and isinstance(nested, str) and nested in {
                "price", "benchmark_price", "distribution", "buy", "sell", "cash_movement",
            }:
                continue
            if key == "notes" and nested == "watch":
                continue
            yield from _structured_texts(nested)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _structured_texts(nested, allow_renderer_labels=allow_renderer_labels)
    elif value is not None:
        yield str(value)


def _validate_structured_safety(
    values: Iterable[Any], *, allow_renderer_labels: bool = False,
) -> None:
    for value in values:
        _validate_finite_values(value)
        for text in _structured_texts(value, allow_renderer_labels=allow_renderer_labels):
            if _normalize_safety_text(text) != text:
                raise ValueError("structured report content contains an invisible or compatibility character")
            _validate_report_safety(text)


def _validate_finite_values(value: Any) -> None:
    if isinstance(value, Real) and not isinstance(value, bool):
        try:
            finite = math.isfinite(float(value))
        except (OverflowError, ValueError):
            finite = False
        if not finite:
            raise ValueError("structured report contains a non-finite numeric value")
    if isinstance(value, dict):
        for nested in value.values():
            _validate_finite_values(nested)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            _validate_finite_values(nested)


def _portfolio_provenance(portfolio: dict[str, Any], name: str) -> dict[str, Any]:
    provenance = portfolio.get("provenance", {}).get(name)
    if not isinstance(provenance, dict) or not provenance.get("derivation"):
        raise ValueError(f"portfolio aggregate {name} is missing provenance")
    return provenance


def _report_contract(
    snapshot: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    provider: str,
    template_version: str,
) -> str:
    contract = json.dumps(
        {
            "candidates": candidates,
            "provider": provider,
            "snapshot": snapshot,
            "template_version": template_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(contract.encode()).hexdigest()


def _render_report_body(
    snapshot: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    provider: str,
    template_version: str,
) -> str:
    """Render only supplied facts and validate the result against the snapshot."""
    lines = [
        "# NISA Quant Assistant Report", "",
        f"Report generated from local snapshot as of `{snapshot['as_of']}`.",
        f"Provider/model identifier: `{provider}`; template: `{template_version}`.",
        f"Report contract fingerprint: `{_report_contract(snapshot, candidates, provider=provider, template_version=template_version)}`.",
        "", "## Data cutoffs", "",
    ]
    for name, cutoff in snapshot["data_cutoffs"].items():
        lines.append(f"- {name}: `{cutoff or 'unavailable'}`")
    portfolio = snapshot["portfolio"]
    lines.extend([
        "", "## Portfolio snapshot", "",
        f"- Market value: `{_number(portfolio['market_value'])}`",
        f"- Cost basis: `{_number(portfolio['cost_basis'])}`",
        f"- Cost basis by currency: `{_mapping_text(portfolio.get('cost_basis_by_currency', {}))}`",
        f"- Contributions: `{_number(portfolio['contributions'])}`",
        f"- Distributions ({portfolio.get('distribution_unit', 'total_cash')}): `{_number(portfolio['distributions'])}`", "",
        f"- Realized P/L by currency: `{_mapping_text(portfolio.get('realized_pl_by_currency', {}))}`", "",
        f"- Portfolio drawdown: `{_number(portfolio.get('drawdown_pct'))}`",
        f"- Portfolio volatility: `{_number(portfolio.get('volatility'))}`", "",
        "Quantities and cost basis are deterministic ledger derivations from cited transaction records.",
        "",
        "Portfolio provenance:",
        "Distribution provenance:",
    ])
    for name in PORTFOLIO_PROVENANCE_FIELDS:
        provenance = _portfolio_provenance(portfolio, name)
        lines.append(f"- {name}: derivation=`{provenance['derivation']}`; sources={_citations_text(list(provenance.get('source_ids', [])))}")
    lines.extend([
        "", "## Data warnings", "",
    ])
    if snapshot["warnings"]:
        lines.extend(f"- `{warning['code']}`: {warning['message']}" for warning in snapshot["warnings"])
    else:
        lines.append("- None recorded.")
    lines.extend([
        "", "## Ranked candidates", "",
        "| Instrument | Account | Label | Evidence | Metrics | Sources |",
        "|---|---|---|---|---|---|",
    ])
    for candidate in candidates:
        metrics = _metrics_text(candidate)
        source_ids = _candidate_source_ids(candidate)
        citations = _citations_text(source_ids)
        lines.append(
            f"| {candidate['instrument']} | {candidate['account']} | {candidate['label']} | "
            f"{candidate['evidence_quality']} | {metrics} | {citations} |"
        )
        lines.extend([
            "", f"**{candidate['instrument']} — {candidate['label']}**",
            f"- Reason: {candidate['reason']}",
            f"- Risk/counter-evidence: {candidate['risk_counter_evidence']}",
            f"- Horizon: {candidate['horizon']}",
            f"- Invalidation/what changes the view: {candidate['invalidation']}",
            f"- {candidate['manual_review']}",
        ])
    lines.extend(["", "## Source list", ""])
    for source in snapshot["sources"]:
        lines.append(
            f"- [{source['id']}] — {source['source_name']} ({source['source_url_or_identifier']}), "
            f"retrieved `{source['retrieved_at']}`, observed `{source['observation_date'] or 'unavailable'}`, "
            f"location `{source['citation_location']}`"
        )
    lines.extend(["", "Manual review required; no order was placed."])
    report = "\n".join(lines) + "\n"
    return report


def render_report(
    snapshot: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    provider: str,
    template_version: str,
) -> str:
    """Render only supplied facts and validate the complete report contract."""
    report = _render_report_body(
        snapshot, candidates, provider=provider, template_version=template_version,
    )
    validate_report(
        report, snapshot=snapshot, candidates=candidates,
        provider=provider, template_version=template_version,
    )
    return report


def _available_source_ids(
    snapshot: dict[str, Any] | None,
    source_records: Iterable[dict[str, Any] | str] | None,
    report: str,
) -> set[str]:
    if snapshot is not None:
        return {str(source["id"]) for source in snapshot.get("sources", []) if "id" in source}
    if source_records is not None:
        return {
            str(record["id"] if isinstance(record, dict) else record)
            for record in source_records
        }
    return set()


def validate_report(
    report: str,
    *,
    snapshot: dict[str, Any] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    source_records: Iterable[dict[str, Any] | str] | None = None,
    provider: str | None = None,
    template_version: str | None = None,
) -> None:
    """Validate Markdown against supplied structured facts and provenance.

    With ``snapshot`` and ``candidates``, ``provider`` and ``template_version``
    are required expected contract values. Ranked candidates and the source
    list must be the canonical renderer structure exactly; unstructured
    validation retains ordinary explanatory-text allowance only for a report
    without renderer markers; renderer-marked reports require provider/model
    and template metadata.  Both paths share fail-closed safety and redaction
    checks.
    """
    materialized_source_records = None if source_records is None else list(source_records)
    original_report = report
    normalized_report = _normalize_safety_text(original_report)
    _validate_report_safety(report)
    _validate_structured_safety((snapshot, materialized_source_records, provider, template_version))
    _validate_structured_safety((candidates,), allow_renderer_labels=True)

    for heading in REQUIRED_HEADINGS:
        if heading not in report:
            raise ValueError(f"report is missing required section: {heading}")
    if "Manual review required; no order was placed." not in report:
        raise ValueError("report is missing the no-order statement")
    if snapshot is not None and candidates is None:
        raise ValueError("structured report validation requires supplied candidates")
    if candidates is not None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("structured report validation requires provider metadata")
        if not isinstance(template_version, str) or not template_version.strip():
            raise ValueError("structured report validation requires template metadata")
    renderer_claimed = (
        snapshot is not None
        or candidates is not None
        or provider is not None
        or template_version is not None
        or any(pattern.search(normalized_report) for pattern in RENDERER_MARKER_PATTERNS)
    )
    actual_provider: str | None = provider
    actual_template: str | None = template_version
    if renderer_claimed:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("renderer report validation requires provider/model metadata")
        if not isinstance(template_version, str) or not template_version.strip():
            raise ValueError("renderer report validation requires template metadata")
        marker_hits = [list(pattern.finditer(report)) for pattern in RENDERER_MARKER_PATTERNS]
        if any(len(matches) != 1 for matches in marker_hits):
            raise ValueError("report renderer markers are missing, malformed, or duplicated")
        generated_lines = [
            line for line in report.splitlines() if RENDERER_GENERATED_LINE.fullmatch(line)
        ]
        metadata_lines = [
            line for line in report.splitlines() if RENDERER_METADATA_LINE.fullmatch(line)
        ]
        fingerprint_lines = [
            line for line in report.splitlines() if RENDERER_FINGERPRINT_LINE.fullmatch(line)
        ]
        if len(generated_lines) != 1 or len(metadata_lines) != 1 or len(fingerprint_lines) != 1:
            raise ValueError("report renderer markers are missing, malformed, or duplicated")
        metadata_match = RENDERER_METADATA_LINE.fullmatch(metadata_lines[0])
        if metadata_match is None or metadata_match.groups() != (provider, template_version):
            raise ValueError("report provider/template metadata is missing or does not match")
    ranked_section = report.split("## Ranked candidates", 1)[1].split("## Source list", 1)[0]
    table_rows = []
    for line in ranked_section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == "Instrument":
            continue
        if cells and all(cell and set(cell) <= {"-", ":"} for cell in cells):
            continue
        if len(cells) < 6 or cells[2] not in LABELS:
            raise ValueError("report contains an unsupported recommendation label")
        table_rows.append("| " + " | ".join(cells) + " |")
    for match in re.finditer(r"\*\*[^\n|]*?\s—\s([^*]+)\*\*", ranked_section):
        if match.group(1).strip() not in LABELS:
            raise ValueError("report contains an unsupported recommendation label")
    for match in re.finditer(r"\b(?:candidate[_ -]?label|label|recommendation)\s*[:=]\s*([^\n,;|]+)", report, re.IGNORECASE):
        if match.group(1).strip() not in LABELS:
            raise ValueError("report contains an unsupported recommendation label")

    if candidates is not None:
        if snapshot is None:
            raise ValueError("structured candidate validation requires a snapshot")
        expected_rows: list[str] = []
        expected_blocks: list[tuple[str, list[str]]] = []
        snapshot_source_set = {str(source["id"]) for source in snapshot.get("sources", []) if "id" in source}
        if f"Report generated from local snapshot as of `{snapshot.get('as_of')}`." not in report:
            raise ValueError("report cutoff does not match supplied snapshot")
        for name, cutoff in snapshot.get("data_cutoffs", {}).items():
            if f"- {name}: `{cutoff or 'unavailable'}`" not in report:
                raise ValueError("report data cutoff does not match supplied snapshot")
        for candidate in candidates:
            if candidate.get("label") not in LABELS:
                raise ValueError("supplied candidate has an unsupported label")
            source_ids = _candidate_source_ids(candidate)
            if not source_ids:
                raise ValueError("candidate has no cited source provenance")
            if not set(source_ids) <= snapshot_source_set:
                raise ValueError("candidate cites a source absent from the supplied snapshot")
            records = [
                *snapshot.get("portfolio", {}).get("holdings", []),
                *snapshot.get("watchlist", []),
            ]
            record = next(
                (
                    item for item in records
                    if item.get("identifier_type") == candidate.get("identifier_type")
                    and item.get("identifier") == candidate.get("identifier_value", candidate.get("instrument"))
                ),
                None,
            )
            if record is None:
                raise ValueError("candidate is absent from the supplied snapshot")
            expected_candidate = _candidate_from_record(
                record,
                account=record.get("account", "watchlist"),
                ledger_source_ids=record.get("ledger_source_ids", []),
            )
            if candidate != expected_candidate:
                raise ValueError("candidate fields or metrics do not match the supplied snapshot")
            relevant_ids = set(record.get("source_ids", [])) | set(record.get("benchmark_source_ids", [])) | set(record.get("ledger_source_ids", []))
            relevant_ids |= set(record.get("distribution_source_ids", []))
            if not set(source_ids) <= relevant_ids:
                raise ValueError("candidate cites an unrelated source")
            expected_rows.append(
                f"| {candidate['instrument']} | {candidate['account']} | {candidate['label']} | "
                f"{candidate['evidence_quality']} | {_metrics_text(candidate)} | {_citations_text(source_ids)} |"
            )
            expected_prose = (
                f"**{candidate['instrument']} — {candidate['label']}**",
                f"- Reason: {candidate['reason']}",
                f"- Risk/counter-evidence: {candidate['risk_counter_evidence']}",
                f"- Horizon: {candidate['horizon']}",
                f"- Invalidation/what changes the view: {candidate['invalidation']}",
                f"- {candidate['manual_review']}",
            )
            if any(line not in ranked_section for line in expected_prose):
                raise ValueError("candidate prose does not match supplied structured candidate")
            prose_source_ids = set(re.findall(rf"\[({SOURCE_ID_PATTERN})\]", "\n".join(expected_prose)))
            if not prose_source_ids <= set(source_ids):
                raise ValueError("candidate prose cites an unrelated source")
            expected_blocks.append((expected_prose[0], list(expected_prose[1:])))
        if table_rows != expected_rows:
            raise ValueError("candidate metrics or provenance do not match supplied candidates")

        actual_blocks: list[tuple[str, list[str]]] = []
        ranked_lines = ranked_section.splitlines()
        line_index = 0
        while line_index < len(ranked_lines):
            line = ranked_lines[line_index].strip()
            if line.startswith("**") and line.endswith("**"):
                body: list[str] = []
                next_index = line_index + 1
                while next_index < len(ranked_lines) and not ranked_lines[next_index].strip():
                    next_index += 1
                while next_index < len(ranked_lines) and ranked_lines[next_index].startswith("- "):
                    body.append(ranked_lines[next_index])
                    next_index += 1
                actual_blocks.append((line, body))
                line_index = next_index
                continue
            line_index += 1
        if actual_blocks != expected_blocks:
            raise ValueError("candidate prose blocks do not match supplied candidates")
        expected_ranked_lines = [
            "| Instrument | Account | Label | Evidence | Metrics | Sources |",
            "|---|---|---|---|---|---|",
        ]
        for row, (heading, body) in zip(expected_rows, expected_blocks):
            expected_ranked_lines.extend((row, heading, *body))
        actual_ranked_lines = [line.strip() for line in ranked_lines if line.strip()]
        if actual_ranked_lines != expected_ranked_lines:
            raise ValueError("ranked candidate section contains unbound or altered content")

        portfolio = snapshot.get("portfolio", {})
        expected_aggregates = (
            f"- Market value: `{_number(portfolio.get('market_value'))}`",
            f"- Cost basis: `{_number(portfolio.get('cost_basis'))}`",
            f"- Cost basis by currency: `{_mapping_text(portfolio.get('cost_basis_by_currency', {}))}`",
            f"- Contributions: `{_number(portfolio.get('contributions'))}`",
            f"- Distributions ({portfolio.get('distribution_unit', 'total_cash')}): `{_number(portfolio.get('distributions'))}`",
            f"- Realized P/L by currency: `{_mapping_text(portfolio.get('realized_pl_by_currency', {}))}`",
            f"- Portfolio drawdown: `{_number(portfolio.get('drawdown_pct'))}`",
            f"- Portfolio volatility: `{_number(portfolio.get('volatility'))}`",
        )
        if any(line not in report for line in expected_aggregates):
            raise ValueError("portfolio aggregate does not match supplied snapshot")
        for name in PORTFOLIO_PROVENANCE_FIELDS:
            provenance = _portfolio_provenance(portfolio, name)
            provenance_ids = set(provenance.get("source_ids", []))
            if not provenance_ids <= snapshot_source_set:
                raise ValueError(f"portfolio provenance for {name} cites an absent source")
            line = f"- {name}: derivation=`{provenance['derivation']}`; sources={_citations_text(list(provenance.get('source_ids', [])))}"
            if line not in report:
                raise ValueError(f"portfolio provenance for {name} is missing or altered")
        if "Distribution provenance:" not in report:
            raise ValueError("distribution provenance is missing")
        source_list = report.split("## Source list", 1)[1]
        listed_ids = re.findall(rf"\[({SOURCE_ID_PATTERN})\]", source_list)
        expected_source_ids = [str(source["id"]) for source in snapshot.get("sources", [])]
        if listed_ids != expected_source_ids:
            raise ValueError("source list does not match supplied snapshot")
        for source in snapshot.get("sources", []):
            expected_source_line = (
                f"- [{source['id']}] — {source['source_name']} ({source['source_url_or_identifier']}), "
                f"retrieved `{source['retrieved_at']}`, observed `{source['observation_date'] or 'unavailable'}`, "
                f"location `{source['citation_location']}`"
            )
            if expected_source_line not in source_list:
                raise ValueError("source metadata does not match supplied snapshot")
        expected_source_lines = [
            f"- [{source['id']}] — {source['source_name']} ({source['source_url_or_identifier']}), "
            f"retrieved `{source['retrieved_at']}`, observed `{source['observation_date'] or 'unavailable'}`, "
            f"location `{source['citation_location']}`"
            for source in snapshot.get("sources", [])
        ] + ["Manual review required; no order was placed."]
        actual_source_lines = [line.strip() for line in source_list.splitlines() if line.strip()]
        if actual_source_lines != expected_source_lines:
            raise ValueError("source list contains unbound or altered content")

        expected_report = _render_report_body(
            snapshot, candidates,
            provider=actual_provider,
            template_version=actual_template,
        )
        if original_report != expected_report:
            raise ValueError("structured report contains unbound or altered content")

    cited_ids = set(re.findall(rf"\[({SOURCE_ID_PATTERN})\]", report))
    available_ids = _available_source_ids(snapshot, materialized_source_records, report)
    if not cited_ids:
        raise ValueError("report has no source citations")
    if not cited_ids <= available_ids:
        missing = sorted(cited_ids - available_ids)
        raise ValueError(f"report cites unavailable source IDs: {missing}")
    has_ledger_candidate = any("| watchlist |" not in row for row in table_rows)
    if has_ledger_candidate and "deterministic ledger derivations from cited transaction records" not in report:
        raise ValueError("report does not identify ledger derivation provenance")
