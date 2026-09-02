# Phase 2 Universe Import Atomicity Repair Implementation Plan

Historical research / deferred — not active implementation instructions
Release boundary: local Phase 2 atomicity repair only; no external execution.
Implemented now: universe import regression tests and SQLite savepoint repair only.
Deferred/not implemented: Phase 3 prediction/verdict work, Hermes/MCP, scheduling, broker access, and trading.
Prohibited: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

> **For agentic workers:** Execute inline in the existing dirty checkout; do not commit or push. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Phase 2 S&P 500 universe CSV import atomic across its member and mapping tables for both commit modes.

**Architecture:** Preserve the existing validation, deterministic IDs, idempotent inserts, membership semantics, scope bindings, and direct error behavior. Put the complete import write sequence inside a SQLite savepoint, retaining an explicit outer transaction when the caller requests `commit=False` on an idle connection, and roll back/release the savepoint on every import exception.

**Tech Stack:** Python 3.11 standard library, SQLite, `unittest`.

**Spec:** `nisa-phase2-repair-r35` task contract.

## Global Constraints

- Preserve the approved Phase 2 standalone read-only evidence/data layer for US-listed S&P 500 constituent equities.
- Do not weaken validation or silently discard errors.
- Do not modify unrelated projects; do not use live providers, credentials, broker APIs, orders, trading, scheduler, MCP, or Hermes.
- Leave implementation uncommitted and unpushed; finish with `status=awaiting_verification`.

### Task 1: Prove and prevent partial universe persistence

**Files:**
- Modify: `src/nisa_quant/phase2.py`
- Modify: `tests/test_phase2.py`

- [x] Write a regression using valid-then-invalid CSV rows and the existing real SQLite schema; exercise in-memory and file-backed databases with `commit=True` and `commit=False`.
- [x] Assert that a raised invalid exchange leaves zero rows in `phase2_universe_inputs`, `phase2_universe_input_members`, and `phase2_universe_members`, including after the caller invokes `commit()`.
- [x] Assert the same connection remains usable by importing a valid CSV, replaying it idempotently, and checking one row in each universe table.
- [x] Wrap the import writes in a savepoint and roll back/release on exceptions while retaining caller-managed transaction semantics.
- [ ] Run the complete unittest, compileall, CLI help, diff check, and targeted import matrix; inspect the dirty diff without committing or pushing.
