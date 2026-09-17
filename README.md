# NISA Quant Assistant

Local-first, advisory-only research tooling for reproducible NISA portfolio evidence and deterministic US-equity ranking experiments.

This v0 package combines a SQLite-backed evidence ledger, offline fixture workflows, point-in-time feature construction, deterministic walk-forward evaluation, content-addressed market-data replay, and Markdown/JSON reports. It is designed for inspection and manual research—not portfolio management or trade execution.

> [!IMPORTANT]
> The project cannot log in to a broker, place orders, or provide an automatic trading path. Model scores and current rankings are research outputs, not recommendations, calibrated probabilities, or evidence of future returns.

## What is implemented

- A dependency-free Python 3.11+ core package and CLI.
- Strict imports for synthetic broker, price, and distribution fixtures.
- Append-only watchlist and recommendation records backed by local SQLite.
- Read-only Phase 2 universe, market, SEC, and RSS evidence collection with explicit provenance and failure records.
- Phase 3 monthly features, three-month excess-return ranking targets, deterministic modeling, walk-forward evaluation, and bound reports.
- Explicit live retrieval from public read-only sources and offline replay from validated local cache artifacts.
- A local stdio MCP adapter for Hermes with a closed two-tool surface.

## v0 limitations

The live Phase 3 universe is the **current** S&P 500 constituent table retrieved from Wikipedia. It is not historical point-in-time membership. Runs built from this universe are current-survivor and survivorship-biased, so benchmark-relative, alpha, excess-return, and historical performance claims are suppressed. Current rankings may still be emitted as descriptive research.

Market history comes from the unofficial Yahoo Finance chart endpoint and remains subject to provider availability and terms. SEC Company Facts are optional, partial, and probed for only one ticker during the bounded live refresh. Missing facts are never treated as issuer-value substitutes.

The project does not include historical index membership, calibrated probability forecasts, paid data, scheduling, broker connectivity, or automated decisions.

## Run locally

The repository uses a `src/` layout and the core package has no third-party runtime dependencies:

```bash
python3 --version
PYTHONPATH=src python3 -m nisa_quant --help
```

An editable environment is optional:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m nisa_quant --help
```

### Offline evidence fixture

This creates a temporary database, imports the checked-in AAPL/MSFT reference subset, and prints its evidence snapshot without network access:

```bash
demo_dir="$(mktemp -d)"
demo_db="$demo_dir/nisa-quant.sqlite"

PYTHONPATH=src python3 -m nisa_quant phase2-refresh-fixtures \
  --db "$demo_db" \
  --universe-csv tests/fixtures/phase2_reference_universe.csv \
  --market-csv tests/fixtures/phase2_reference_market.csv \
  --as-of 2026-09-04 \
  --request-id phase2-reference-demo-1

PYTHONPATH=src python3 -m nisa_quant phase2-evidence \
  --db "$demo_db" \
  --as-of 2026-09-04
```

The fixture observations are historical at that cutoff and are audited accordingly; the evidence path emits no prediction or trade action.

### Phase 3 live refresh and replay

Replay is the default and never performs network retrieval. It requires exactly one compatible cache artifact whose canonical request contract and date range match the request.

Use `--live` to explicitly allow public read-only retrieval. The following is a limited smoke run, not a full-universe run:

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

Omit `--limit` for the full current-universe path. Add `--sec-contact researcher@example.com` only when explicitly requesting the optional SEC probe; it must be a real, non-secret contact email.

After a complete compatible live retrieval has populated the cache, replay the same range offline:

```bash
PYTHONPATH=src python3 -m nisa_quant phase3-refresh \
  --as-of 2026-09-16 \
  --start 2024-01-01 \
  --end 2026-09-16 \
  --cache-dir data/phase3 \
  --output reports/phase3/report.json \
  --replay-only
```

Passing neither mode flag also selects replay. Passing both is invalid. `--limit` is live-only, and partial provider snapshots are not saved as replayable cache artifacts.

## Status and exit contract

| Status | Exit | Meaning |
| --- | ---: | --- |
| `available` | 0 | Validated output with point-in-time membership evidence. |
| `available_descriptive` | 0 | Validated descriptive output; evidence policy still suppresses unsupported performance claims. |
| `unavailable_insufficient_data` | 2 | Processing completed, but there were no eligible validation periods or selections. Current descriptive predictions may still be present. |
| `unavailable` | 2 | A range, cache, provider, coverage, or artifact validation failure produced a structured unavailable report. |

Exit code `2` is therefore not necessarily a crash. Inspect the report and adjacent manifest for the reason, gaps, per-ticker failures, freshness evidence, and claim-suppression flags.

## Outputs

- CLI database commands default to `data/nisa_quant.sqlite`.
- Phase 3 history caches are written as `data/phase3/history-<request-hash>.json` only after a complete retrieval.
- `phase3-refresh --output PATH` writes `PATH` and an atomic companion `PATH.manifest.json`.
- Reports contain artifact identifiers, source/request provenance, coverage, prediction freshness, SEC status, limitations, and the manual-only boundary.
- Generated `data/` and `reports/` content, database files, environments, credentials, and private input directories are ignored by Git.

## Hermes MCP tools

The stdio adapter at `tools/nisa_quant_mcp_server.py` is intended to run with the Hermes virtual environment, which supplies the MCP SDK:

```bash
hermes mcp add nisa_quant \
  --command /Users/user/.hermes/hermes-agent/venv/bin/python \
  --args /Users/user/Documents/Yume/nisa-research/tools/nisa_quant_mcp_server.py
```

Restart Hermes or start a new session after registration so it can discover:

- `nisa_quant_refresh` — accepts `as_of`, `start`, `end`, `mode` (`replay` by default), optional live-only `limit` (maximum 50), optional live-only `sec_contact`, and `format` (`markdown` or `json`).
- `nisa_quant_latest` — reads the newest fixed Hermes report/manifest pair and returns bounded report content without refreshing.

The adapter fixes cache access to `data/phase3` and output to `reports/phase3/hermes-report.{markdown,json}` plus the adjacent manifest. Callers cannot supply arbitrary paths. Results summarize status, predictions, freshness, SEC coverage, gaps, and failures while redacting local paths and secret-shaped values and bounding collection/content sizes.

In addition to producer statuses, MCP may return `validation_error`, `producer_error`, `not_yet_run`, or `incomplete_latest`. A completed `unavailable_insufficient_data` response is a valid structured tool result, not an MCP transport error.

## Safety boundaries

- All provider access is read-only; replay performs no network calls.
- Live access must be explicit and is restricted to vetted public HTTPS providers with redirect and private-address defenses.
- There is no broker authentication, account mutation, arbitrary command, arbitrary path, SQL, or order tool in the Hermes adapter.
- Reports fail closed on malformed, stale, conflicting, future-dated, non-finite, unbound, or insufficient evidence.
- Content-derived identities bind history, datasets, models, backtests, and reports; replay validates the requested range and artifact contract rather than relabeling cached data.
- Reports and MCP responses preserve provider failures and unsupported claims instead of fabricating values.
- Real broker exports, credentials, account identifiers, and personal reports must stay out of source control.

## Development checks

The test suite is offline except for an optional Hermes stdio smoke test, which skips when the tested Hermes environment is unavailable:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests tools
```

Useful focused checks:

```bash
PYTHONPATH=src python3 -m unittest tests.test_cli -v
PYTHONPATH=src python3 -m unittest tests.test_nisa_quant_mcp -v
```
