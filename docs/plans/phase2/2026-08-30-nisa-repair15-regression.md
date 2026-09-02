# Historical research / deferred — not active implementation instructions
Release boundary: local test determinism repair only; no external execution.
Implemented now: local fixture/CSV/SQLite/CLI only.
Deferred/not implemented: Hermes runtime integration, live providers, scheduling, and delivery.
Prohibited boundaries: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

# NISA Repair15 Regression Revision Implementation Plan

> **For agentic workers:** This is a local-only repair plan. Work in the existing checkout; do not reset, clean, checkout, commit, or push. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the legacy supervisor tests deterministic under current-UTC retrieval and watchlist effective-date semantics without weakening production chronology checks.

**Architecture:** Keep production `utc_now()`, `_available_by`, and point-in-time watchlist selection unchanged. Add one test-only helper that returns the current UTC calendar date, use it only after test data is created, and preserve explicit earlier/future cutoff assertions in chronology tests.

**Tech Stack:** Python 3 standard library, `unittest`, SQLite, existing local fixtures.

**Spec:** User request: “NISA Quant Assistant — repair15 regression revision after supervisor verification”.

## Global Constraints

- Work only in `/Users/user/Documents/Yume/nisa-research`.
- No network/live providers, credentials, personal data, broker login/write/order execution, scheduling/dispatch, Discord delivery, or Yume iOS changes.
- Do not weaken `_available_by`, bypass cutoff checks, or replace production `utc_now()` with a fixed date.
- Preserve repair15 regressions for migration, report safety, embedded identifiers, credential/filename patterns, and renderer binding.
- Leave the tree uncommitted and available for supervisor review.

### Task 1: Deterministic current-cutoff test helper

**Files:**
- Create: `tests/test_time_helpers.py`
- Test: `tests/test_metrics_screens.py`, `tests/test_reports_journal.py`, `tests/test_repairs.py`, `tests/test_ninth_repair.py`

**Interfaces:**
- `current_utc_date() -> str` returns `datetime.now(timezone.utc).date().isoformat()` for test cutoffs only.
- Existing production APIs and chronology assertions remain unchanged.

- [ ] **Step 1: Confirm the stale-anchor RED state**

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and record that the six observed failures/errors result from `2026-08-28` being earlier than current-day retrieval/effective dates.

- [ ] **Step 2: Add the test-only helper**

Create `tests/test_time_helpers.py` with only:

```python
from datetime import datetime, timezone


def current_utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()
```

- [ ] **Step 3: Replace only stale current-data anchors**

In fixture-based metric and report setup, call `current_utc_date()` after imports and use that value for snapshot/screen cutoffs and journal `data_cutoff` expectations. In the undated watchlist test, call it after `add_watchlist_item`. In the legacy migration test, call it after `initialize_database`. Keep explicit earlier cutoff assertions, future observations, and historical tests unchanged.

- [ ] **Step 4: Run focused tests**

Run the affected test classes and `tests.test_fifteenth_repair`, `tests.test_fourteenth_repair`, `tests.test_eleventh_repair`, `tests.test_twelfth_repair`, and `tests.test_thirteenth_repair` with `PYTHONPATH=src python3 -m unittest ... -v`; expect all to pass without production changes.

### Task 2: Complete verification and boundary audit

**Files:**
- No production files.

- [ ] **Step 1: Run the full required suite and syntax checks**

Run the full unittest discovery, compileall, and CLI help commands from the user request.

- [ ] **Step 2: Run focused repair15 and file-backed E2E checks**

Run focused repair15 tests, a fresh `mktemp`/`trap` file-backed CLI workflow, and inspect outputs for local-only behavior.

- [ ] **Step 3: Run static and boundary scans**

Run `git diff --check`, AST parsing, secret/scope/deferred-boundary scans, Yume iOS status, and final `git status --short`. Report exact failures if any remain.
