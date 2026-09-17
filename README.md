# NISA Quant Assistant

A local-first, advisory-only research assistant for reproducible NISA-oriented US-equity analysis. It turns validated market evidence into deterministic rankings and inspectable reports while keeping data, caches, and generated artifacts on your machine.

> [!IMPORTANT]
> This is a research tool, not a trading system. It cannot sign in to a broker, place or prepare orders, or make automatic investment decisions. Rankings and scenario ranges are not recommendations, calibrated probabilities, or promises of future returns. A person must review the evidence and make every decision.

## What it does

- Imports documented local fixtures into a SQLite evidence ledger for portfolio snapshots, screens, watchlists, and recommendation records.
- Builds monthly features, three-month excess-return targets, deterministic model artifacts, walk-forward evaluations, and current candidate rankings.
- Replays validated, content-addressed history caches without network access by default.
- Retrieves public market evidence only when live mode is explicitly requested.
- Writes JSON or Markdown reports with adjacent manifests describing provenance, coverage, freshness, failures, artifact identities, and suppressed claims.
- Exposes a small, fixed-path Hermes MCP surface for refreshing research or reading the latest result.

## Requirements

- Python 3.11 or later
- No third-party runtime dependencies for the core CLI
- The Hermes Python environment and MCP SDK only when using the Hermes adapter

From the repository root:

```bash
python3 --version
PYTHONPATH=src python3 -m nisa_quant --help
```

An editable virtual environment is optional:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m nisa_quant --help
```

## Run a research refresh

The CLI command is named `phase3-refresh`; this is a command name, not a project lifecycle label. Replay is the default: it performs no network retrieval and requires exactly one compatible cache artifact for the requested date range.

```bash
PYTHONPATH=src python3 -m nisa_quant phase3-refresh \
  --as-of 2026-09-16 \
  --start 2024-01-01 \
  --end 2026-09-16 \
  --cache-dir data/phase3 \
  --output reports/phase3/report.json
```

To populate the cache from public, read-only sources, opt in with `--live`:

```bash
PYTHONPATH=src python3 -m nisa_quant phase3-refresh \
  --as-of 2026-09-16 \
  --start 2024-01-01 \
  --end 2026-09-16 \
  --cache-dir data/phase3 \
  --output reports/phase3/report.json \
  --live \
  --limit 3
```

`--limit` is a live-only way to run a small subset; omit it to request the full current universe. Optional SEC Company Facts retrieval also requires live mode and a real, non-secret contact string supplied with `--sec-contact researcher@example.com`; the bounded live probe covers only the first selected ticker. Passing both `--live` and `--replay-only` is invalid.

> [!NOTE]
> Live retrieval uses the current S&P 500 constituent table from Wikipedia and market history from the unofficial Yahoo Finance chart endpoint. Provider availability and terms can change.

## Outputs

For `--output reports/phase3/report.json`, the refresh writes:

- `reports/phase3/report.json` — the requested JSON research report. Use a `.md` or `.markdown` output path for Markdown.
- `reports/phase3/report.json.manifest.json` — the atomic companion manifest with request provenance, coverage, freshness, per-ticker gaps and failures, SEC status, and bound artifact identifiers.
- `data/phase3/history-<request-hash>.json` — a reusable history cache created only from a complete compatible live retrieval.

Generated `data/`, `reports/`, database files, environments, credentials, and private input directories are ignored by Git.

### Status and exit codes

| Status | Exit code | Meaning |
| --- | ---: | --- |
| `available` | 0 | Validated output backed by point-in-time membership evidence. |
| `available_descriptive` | 0 | Validated descriptive output with unsupported performance claims suppressed. |
| `unavailable_insufficient_data` | 2 | Processing completed, but there were no eligible validation periods or selections; current descriptive predictions may still be present. |
| `unavailable` | 2 | A range, cache, provider, coverage, or artifact validation failure produced a structured unavailable report. |

Exit code `2` does not always mean the process crashed. Read the report and manifest for the exact reason and retained evidence.

## Hermes integration

Register the stdio adapter with Hermes using the Python interpreter from the Hermes environment and the absolute path to `tools/nisa_quant_mcp_server.py`:

```bash
hermes mcp add nisa_quant \
  --command /path/to/hermes-agent/venv/bin/python \
  --args /absolute/path/to/nisa-research/tools/nisa_quant_mcp_server.py
```

Restart Hermes or open a new session after registration. The adapter exposes only:

- `nisa_quant_refresh` — runs a replay by default or an explicitly requested live refresh. It accepts `as_of`, `start`, `end`, `mode`, optional live-only `limit` (maximum 50), optional live-only `sec_contact`, and `format` (`markdown` or `json`).
- `nisa_quant_latest` — reads the newest complete Hermes report and manifest pair without refreshing.

Hermes cache access is fixed to `data/phase3`; reports are fixed to `reports/phase3/hermes-report.{markdown,json}` with adjacent manifests. Callers cannot provide arbitrary paths. Results are bounded and redact local paths and secret-shaped values. Adapter statuses can also include `validation_error`, `producer_error`, `not_yet_run`, and `incomplete_latest`; `unavailable_insufficient_data` remains a completed structured result rather than a transport error.

## Safety and limitations

- Replay is offline by default. Network retrieval happens only through explicit live mode and vetted read-only public HTTPS providers.
- The live universe contains current S&P 500 survivors, not historical point-in-time membership. This creates survivorship bias, so historical performance, alpha, benchmark-relative, and excess-return claims are suppressed for those runs; rankings remain descriptive research output.
- SEC Company Facts are optional and may be partial. Missing facts stay explicit and are never replaced with invented issuer values.
- Reports fail closed on malformed, stale, conflicting, future-dated, non-finite, unbound, or insufficient evidence.
- The Hermes adapter has no broker authentication, account mutation, order, trade-execution, arbitrary command, arbitrary path, or SQL tool.
- Keep real broker exports, credentials, account identifiers, and personal reports out of source control.
- Final interpretation and every investment action remain manual.
