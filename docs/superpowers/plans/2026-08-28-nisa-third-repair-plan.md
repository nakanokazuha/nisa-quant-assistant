# Historical research / deferred — not active implementation instructions

Release boundary: Implemented now: local fixture/CSV/SQLite/CLI only. Deferred/not implemented: Hermes runtime integration, live providers, scheduling, and delivery. Prohibited boundaries: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

# NISA Quant Assistant Third Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make historical watchlists, source identity, portfolio risk, reports, and recommendation outcomes deterministic, provenance-safe, and point-in-time correct.

**Architecture:** Preserve the local SQLite ledger and no-order boundary. Add append-only watchlist versions, typed benchmark metadata, and deterministic source grouping; derive portfolio metrics only from common dated observations and fail closed when required data is unavailable. Validate reports and journal records against structured snapshots and cited source rows before persistence.

**Tech Stack:** Python 3, stdlib `sqlite3`, `unittest`, Markdown rendering, canonical JSON/SHA-256.

**Spec:** `NISA_QUANT_ASSISTANT_SPEC.md` and the user-provided third-repair requirements.

## Global Constraints

- Local-only synthetic data; no network, credentials, broker login/write/order execution, scheduling, Discord delivery, or Yume iOS changes.
- Strict TDD: regression test, observe RED, minimal root-cause fix, observe GREEN, then full suite.
- Keep changes uncommitted and work only in `/Users/user/Documents/Yume/nisa-research`.
- Preserve typed identifiers and declared benchmark identity; never infer aliases or global benchmarks.
- Preserve explicit unavailable/conflicting states and all source/cutoff provenance.

### Task 1: Historical metadata and deterministic source identity

**Files:**
- Modify: `src/nisa_quant/schema.py`, `src/nisa_quant/watchlist.py`, `src/nisa_quant/metrics.py`, `src/nisa_quant/screens.py`
- Test: `tests/test_repairs.py`

- [ ] Write tests for adding/editing watchlist versions, latest authoritative identical refreshes, conflicting same-date values, undeclared benchmarks, and typed benchmark propagation.
- [ ] Run the targeted tests and observe failures against mutable watchlist/global benchmark/lexicographic source selection.
- [ ] Add append-only version rows and point-in-time selection, including typed benchmark columns and migration-safe initialization.
- [ ] Select the latest retrieval for identical values; mark disagreement conflicting and exclude all derived metrics.
- [ ] Run the targeted tests and confirm they pass.

### Task 2: Portfolio-level risk and fail-closed aggregation

**Files:**
- Modify: `src/nisa_quant/metrics.py`
- Test: `tests/test_repairs.py`

- [ ] Write tests for common-date alignment, returned portfolio drawdown, and conflicting/partial holding data.
- [ ] Run them RED.
- [ ] Build dated portfolio returns only on common comparable dates, expose drawdown, and never copy a holding volatility; retain known-by-currency values while making aggregate values unavailable when required data is missing.
- [ ] Run them GREEN.

### Task 3: Structured report provenance and redaction

**Files:**
- Modify: `src/nisa_quant/reports.py`
- Test: `tests/test_repairs.py`

- [ ] Write tests for unsupported prose labels, unrelated/invented source IDs, altered metrics/reasons/aggregates, missing ledger/distribution derivation, and secret/account/PII filename redaction with ordinary explanatory `token` allowed.
- [ ] Run them RED.
- [ ] Validate the supplied snapshot/candidates structurally, require candidate-local citations and explicit derivation references, and redact only secret-shaped/account/PII values.
- [ ] Run them GREEN.

### Task 4: Strict journal validation

**Files:**
- Modify: `src/nisa_quant/journal.py`, `src/nisa_quant/schema.py`
- Test: `tests/test_repairs.py`, `tests/test_reports_journal.py`

- [ ] Write tests for future/mismatched cutoffs, altered candidate fields, invented/uncited sources, future and stale/conflicting outcomes, wrong typed benchmark/currency, and one valid later outcome.
- [ ] Run them RED.
- [ ] Validate exact generated snapshots/candidates, typed metadata, source freshness/cutoffs, benchmark identity, and cited outcome records before writing canonical JSON/hash.
- [ ] Run them GREEN.

### Task 5: Documentation and final verification

**Files:**
- Modify: `README.md` and module/schema comments.

- [ ] Document versioned watchlist metadata, typed benchmarks, source refresh policy, aligned portfolio risk, report provenance, and journal evidence rules.
- [ ] Run the complete unittest, compileall, CLI help, fresh temp-directory E2E, adversarial probes, `git diff --check`, AST parse, secret/scope scan, and final status.
