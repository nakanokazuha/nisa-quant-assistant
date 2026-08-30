# Historical research / deferred — not active implementation instructions
Release boundary: local repair plan only; no external execution.
Implemented now: local fixture/CSV/SQLite/CLI only.
Deferred/not implemented: Hermes runtime integration, live providers, scheduling, and delivery.
Prohibited boundaries: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

# NISA Twelfth Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four release-review safety gaps without changing the local-only, no-order execution boundary.

**Architecture:** Keep the existing append-only watchlist, source-record, snapshot, journal, and report layers. Add shared fail-closed predicates at source ingestion and snapshot boundaries, make legacy migration seed a current version only when no safe current version exists, and scan all report-bound structured values before canonical binding.

**Tech Stack:** Python 3, SQLite, stdlib `unittest`, Markdown renderer.

**Spec:** User request: “NISA Quant Assistant — twelfth repair after release review”.

## Global Constraints

- Work only in `/Users/user/Documents/Yume/nisa-research`.
- No dependencies, network/live providers, credentials, broker login/write/order execution, scheduling, Discord delivery, or Yume iOS changes.
- Strict TDD: regression RED, minimal fix, GREEN, then full checks.
- Leave the tree dirty and uncommitted.

### Task 1: Watchlist migration safety

**Files:**
- Modify: `src/nisa_quant/schema.py`, `src/nisa_quant/watchlist.py`
- Test: `tests/test_twelfth_repair.py`

- [ ] Add a legacy materialized row plus a future-only typed version before initialization; assert initialization and repeat initialization retain the current row, materialize no future metadata, and expose legacy-before/future-after through `watchlist_as_of`.
- [ ] Run that test and observe the deletion/future-only failure.
- [ ] Make migration determine whether a version is safe at the current cutoff, seed missing legacy current history, and keep future versions append-only.
- [ ] Run the focused test and existing watchlist tests.

### Task 2: Typed source facts and malformed dates

**Files:**
- Modify: `src/nisa_quant/sources.py`, `src/nisa_quant/metrics.py`, `src/nisa_quant/journal.py`, `src/nisa_quant/watchlist.py`, `src/nisa_quant/imports.py`, existing valid fixtures/tests as needed
- Test: `tests/test_twelfth_repair.py`

- [ ] Add direct database regressions for all accepted unit mappings, rejected blank/mismatched mappings, invalid legacy source rows excluded from market data/history/provenance, and malformed observation/retrieval/trade/movement/watchlist dates ignored without aborting snapshots.
- [ ] Run the tests and observe RED.
- [ ] Define one closed field/unit contract and use it independently in ingestion, source grouping, transaction/cash validation, and outcome validation.
- [ ] Validate dates before comparisons or age/history arithmetic, retaining audit rows and emitting warnings where snapshot context exists.
- [ ] Run focused and existing fixture tests.

### Task 3: Report safety

**Files:**
- Modify: `src/nisa_quant/reports.py`
- Test: `tests/test_twelfth_repair.py`

- [ ] Add exact standalone/embedded imperative, identifier, PII filename, credential, structured-field, source metadata, instrument, and citation adversarial cases plus valid renderer cases.
- [ ] Run the report tests and observe RED.
- [ ] Replace label-stripping bypasses with a shared scan over canonical and supplied structured values; preserve required provider/template binding.
- [ ] Run focused and existing report/journal tests.

### Task 4: Verification

**Files:**
- No production changes expected.

- [ ] Run the full unittest discovery, compileall, CLI help, fresh file-backed E2E, focused probes, diff check, AST parse, secret/scope/deferred-boundary scan, yume-ios status, and final status.
- [ ] Record only observed results in the handoff; do not commit or push.
