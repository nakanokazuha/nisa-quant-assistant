# Historical research / deferred — not active implementation instructions
Release boundary: local repair plan only; no external execution.
Implemented now: local fixture/CSV/SQLite/CLI only.
Deferred/not implemented: Hermes runtime integration, live providers, scheduling, and delivery.
Prohibited boundaries: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

# NISA Quant Assistant Eleventh Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

---

### Task 1: Current watchlist projection and resumable migration

**Files:**
- Modify: `src/nisa_quant/watchlist.py`
- Modify: `src/nisa_quant/database_schema.py`
- Test: `tests/test_eleventh_repair.py`

**Interfaces:**
- `add_watchlist_item` continues appending an idempotent version and projects only the latest version effective and observed by the current UTC materialization date.
- `initialize_database` migrates missing legacy rows on every initialization and rebuilds `watchlist` and existing `instruments` from the safe current projection.

- [ ] **Step 1: Write failing tests** for a current version followed by a 2099 version, reinitialization, materialized-table inspection, and pre-future `watchlist_as_of`; for an existing empty version table with a legacy row; for partial migration; and for repeat initialization.
- [ ] **Step 2: Run only those tests** with `PYTHONPATH=src python3 -m unittest tests.test_eleventh_repair.WatchlistMaterializationTests -v` and confirm RED because future metadata is projected and migration is not resumed.
- [ ] **Step 3: Implement the smallest projection helper** that selects `watchlist_as_of` at the current UTC date, deletes/rebuilds only the materialized watchlist rows represented by the version store, updates existing typed instruments without creating future instruments, and leaves append-only versions intact.
- [ ] **Step 4: Implement resumable legacy migration** using detected legacy effective/observed columns when valid; otherwise seed the current UTC date and an aware UTC observed timestamp under an explicit “no historical metadata” policy. Use idempotent typed-key/effective-date checks and never use an ancient sentinel date.
- [ ] **Step 5: Run the focused watchlist tests** and confirm GREEN, then rerun the existing watchlist/schema tests.

---

### Task 2: Typed source-bound transaction and cash replay

**Files:**
- Modify: `src/nisa_quant/portfolio_metrics.py`
- Modify: `src/nisa_quant/source_records.py` only if the shared usability contract needs a narrowly scoped helper
- Test: `tests/test_eleventh_repair.py`

**Interfaces:**
- Snapshot replay accepts a transaction only when its linked source has the expected field, usable recognized freshness, compatible unit/currency, finite valid value, exact typed instrument identity, valid source/trade chronology, and a value matching the represented ledger amount.
- Cash replay accepts only `cash_movement` sources with usable provenance, `amount` unit, compatible currency, finite matching amount, valid movement/observation/retrieval dates, and source retrieval on or before the snapshot cutoff.
- Invalid rows remain in audit/source tables and produce explicit warning codes without changing accounting or provenance.

- [ ] **Step 1: Write direct SQLite regression probes** for stale BUY, wrong source field, wrong source value, wrong typed identity, malformed observation/trade date, non-finite cash, a price source attached to cash, and valid BUY/SELL/distribution/cash rows.
- [ ] **Step 2: Run only the source-binding tests** and confirm RED because current replay checks only a subset of dates/identity/value rules.
- [ ] **Step 3: Implement typed validators** with guard clauses and explicit warning codes, then make the ledger and cash loops inspect malformed rows before accounting.
- [ ] **Step 4: Run the focused source-binding tests** and confirm GREEN, including same-currency fixture import/replay and distribution behavior.

---

### Task 3: Report safety and renderer contract binding

**Files:**
- Modify: `src/nisa_quant/report_rendering.py`
- Modify: `docs/README.md`
- Test: `tests/test_eleventh_repair.py`
- Modify: `tests/test_reports_journal.py` only where the new explicit contract is tested

**Interfaces:**
- Safety scanning rejects normalized standalone and label-containing action imperatives, account/customer/broker/portfolio identifiers, PII-like filenames, and common credential shapes while allowing canonical renderer labels only at their exact table/heading locations, ordinary safe filenames, and explanatory `token`.
- Unsafe candidate/source/instrument fields cannot bypass scanning because safety runs on the complete report before label handling and structured validation binds canonical fields.
- Renderer-bound validation requires nonblank provider/model and template metadata; manual unstructured validation remains supported only for reports without renderer markers and requires source citations.

- [ ] **Step 1: Write exact adversarial tests** for all named action phrases/separator variants, identifier forms, filenames, credential shapes, unsafe structured fields, missing provider/template renderer metadata, and a valid canonical renderer report.
- [ ] **Step 2: Run only the report safety tests** and confirm RED on currently accepted phrases/bypasses.
- [ ] **Step 3: Expand normalization/pattern coverage** and enforce the explicit renderer/manual validation distinction with minimal changes.
- [ ] **Step 4: Run the focused report tests** and confirm GREEN, then rerun all existing report/journal tests.

---

### Task 4: Documentation and complete verification

**Files:**
- Modify: `docs/README.md`
- Modify: relevant module comments/docstrings in `src/nisa_quant/database_schema.py`, `src/nisa_quant/watchlist.py`, `src/nisa_quant/portfolio_metrics.py`, and `src/nisa_quant/report_rendering.py` as needed
- Test: `tests/test_eleventh_repair.py`

- [ ] **Step 1: Add regression comments/assertions** that state the materialization, migration, source-binding, and report contracts.
- [ ] **Step 2: Run the complete unittest discovery command.**
- [ ] **Step 3: Run compileall, CLI help, fresh file-backed E2E with `mktemp` and `trap`, four focused in-memory probes, `git diff --check`, AST parsing, secret/scope/deferred-boundary scan, Yume iOS status, and final Git status.
- [ ] **Step 4: Inspect all outputs and report only observed results; leave the implementation uncommitted.**
