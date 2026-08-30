# Historical research / deferred — not active implementation instructions

Release boundary: Implemented now: local fixture/CSV/SQLite/CLI only. Deferred/not implemented: Hermes runtime integration, live providers, scheduling, and delivery. Prohibited boundaries: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

# NISA Quant Assistant Fifth Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the seven final-release review findings while preserving the local-only, advisory-only, fail-closed NISA ledger contract.

**Architecture:** Keep SQLite and the existing public APIs. Add shared source metadata validation at insertion and fixture boundaries, make all historical selection depend on typed records and cutoffs, canonicalize broker transactions before ledger application, and validate structured reports by exact parsed rows and bound facts. Extend portfolio accounting with per-currency maps while leaving mixed-currency aggregates unavailable without FX.

**Tech Stack:** Python 3 standard library, `sqlite3`, `unittest`, Markdown, AST/compile checks; no new dependencies.

**Spec:** `NISA_QUANT_ASSISTANT_SPEC.md` plus the fifth final-release repair requirements supplied by the user.

## Global Constraints

- Work only in `/Users/user/Documents/Yume/nisa-research`; use `--allow-dirty` semantics and leave changes uncommitted.
- Strict TDD: each repair begins with a focused regression test observed RED, followed by a minimal root-cause fix and GREEN verification.
- No network/live providers, credentials, personal data, broker login/write/order execution, scheduling/dispatch, Discord, or Yume iOS changes.
- Preserve strict CSV headers, duplicate/idempotent behavior, unknown-account quarantine, typed identifiers, average-cost accounting, provenance, and safe unavailable states.

### Task 1: Historical metadata and source metadata fail-closed validation

**Files:** `tests/test_repairs.py`, `src/nisa_quant/sources.py`, `src/nisa_quant/metrics.py`, `src/nisa_quant/watchlist.py`, `README.md`

- [x] Add RED tests for future watchlist metadata not leaking into a historical holding, rejected/quarantined missing unit/currency/arbitrary freshness, stale distributions and benchmarks being unusable, and preserved warnings/provenance.
- [x] Run only those tests and record the expected failures.
- [x] Implement explicit recognized freshness statuses, field-specific required metadata, safe historical instrument fallback, and fail-closed source grouping/metrics.
- [x] Run the focused tests GREEN and update documentation.

### Task 2: Chronological broker import

**Files:** `tests/test_repairs.py`, `src/nisa_quant/imports.py`

- [x] Add a reverse-ordered BUY/SELL regression asserting both rows are accepted, average-cost quantity/basis is correct, and realized P/L is retained.
- [x] Run it RED.
- [x] Parse and validate all rows first, sort accepted transaction applications by transaction date and stable source row identity, and preserve original row/audit identifiers.
- [x] Run importer tests GREEN and then the existing import suite.

### Task 3: Canonical structured report validation

**Files:** `tests/test_repairs.py`, `src/nisa_quant/reports.py`, `README.md`

- [x] Add RED tests for the exact uncited injection, valid explanatory prose, altered metrics, invented sources, extra/missing candidate rows, unsupported actions in free prose, secret-shaped values, and ordinary explanatory `token`.
- [x] Run focused report tests RED.
- [x] Parse the ranked table and candidate blocks into canonical representations, compare exact candidate order/content/source relevance, and bind numeric/factual claims to supplied facts or deterministic derivations while keeping redaction narrow.
- [x] Run report tests GREEN and document the contract.

### Task 4: Cited outcome evidence gate

**Files:** `tests/test_repairs.py`, `src/nisa_quant/journal.py`

- [x] Add RED regression for an unrelated later distribution not authorizing an evaluation with January 2 cited price/benchmark, plus one valid later evidence case.
- [x] Run it RED.
- [x] Replace broad latest-date scanning with validation of the two cited source records only, including exact typed identity, fields, currency, freshness, conflicts, observation/retrieval/cutoff/evaluation ordering.
- [x] Run journal tests GREEN.

### Task 5: Snapshot warning and currency accounting

**Files:** `tests/test_repairs.py`, `src/nisa_quant/metrics.py`, `README.md`

- [x] Add RED tests excluding undated historical warnings, retaining dated cutoff behavior, and asserting deterministic `cost_basis_by_currency` / `realized_pl_by_currency` with unavailable mixed-currency aggregates.
- [x] Run them RED.
- [x] Exclude NULL-observation warnings from point-in-time snapshots and expose exact per-currency ledger maps/provenance independently of aggregate availability.
- [x] Run focused and full checks GREEN.

### Task 6: Final verification

- [x] Run the complete unittest suite, compileall, CLI help, fresh `mktemp`/`trap` fixture E2E, all seven in-memory adversarial probes, `git diff --check`, AST parsing, secret/scope scan, and final `git status`.
- [x] Confirm no implementation commit was created and report observed evidence only.
