# NISA Phase 2 Repair r47 Implementation Plan

Historical research / deferred — not active implementation instructions
Release boundary: local Phase 2 repair plan only.
Implemented now: local read-only SQLite/provider boundary and tests.
Deferred roadmap: live providers, scheduling, delivery, and integrations.
Prohibited: credentials, broker login/write/order execution, and Yume iOS changes.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the Phase 2 persistence boundary so market and evidence batches are atomic, caller-owned evidence transactions remain rollbackable, immutable scope cutoffs and exact direct membership are enforced, and unsafe request IDs are rejected before persistence.

**Architecture:** Keep the existing standalone read-only Phase 2 providers, canonical identities, request-bound scopes, bindings, conflict discovery, and evidence-only reports. Add one exact-target resolver for implicit direct scopes; validate complete market responses into memory before entering a rollbackable batch unit; start an outer SQLite transaction when a caller-managed operation is idle; and validate report/evidence dates against the immutable scope cutoff.

**Tech Stack:** Python 3, `sqlite3`, `unittest`, existing Phase 2 provider and scanner contracts.

**Spec:** User-provided Consolidated Phase 2 Repair Contract `nisa-phase2-repair-r47`, based on architecture closure audit `nisa-phase2-architecture-audit-r46`.

## Global Constraints

- Preserve the approved standalone Phase 2 read-only evidence/data layer for US-listed S&P 500 constituent equities.
- Do not modify `/Users/user/Documents/Yume/yume-ios` or unrelated projects.
- No live providers, credentials, broker APIs, orders, trading, scheduler, MCP, or Hermes integration.
- No raw secrets, private URLs, or PII in source/docs/logs; redact as `[REDACTED]`.
- Leave implementation uncommitted and unpushed.
- Preserve request-to-scope immutability, exact membership, request-bound conflicts/reports, replay fingerprints, batch atomicity, cutoff/chronology/freshness, provenance, scanner, DNS, rebinding, redirect, and migration safety.

### Task 1: Add red regressions for the consolidated invariants

**Files:**
- Modify: `tests/test_phase2.py`

**Interfaces:**
- Consumes: Existing `new_connection`, `seed_universe`, `universe_row`, `market_row`, `evidence_record`, provider dataclasses, and Phase 2 public functions.
- Produces: Regressions proving market valid-then-invalid rollback for `commit=True` and `commit=False`, evidence idle `commit=False` caller rollback with a pre-existing scope, future evidence/report cutoff isolation, exact ABC-vs-XYZ direct scopes, and request-ID safety rejection before any scope/binding/failure/run/snapshot persistence.

- [ ] **Step 1: Add the failing tests.**
  - Use an in-memory and file-backed connection for the evidence idle transaction case.
  - Use a two-member universe for direct ABC/XYZ isolation and assert both scope membership and report output.
  - Use unsafe request IDs shaped as token, credential, broker/account, and directive values; assert the generic validation error does not contain the rejected value and all Phase 2 persistence tables remain empty.
  - Use an older explicit scope and a record retrieved/published after that scope cutoff; assert ingestion rejects before evidence/binding persistence and a later report remains unchanged.
  - Use a market provider returning a valid ABC observation followed by an invalid ABC observation; assert zero market rows/bindings and exactly one batch failure for both commit modes.
- [ ] **Step 2: Run only the new tests and verify they fail for the audit findings.**

Run:

```bash
python3 -m unittest tests.test_phase2.Phase2ReviewRegressionTests.<new_test_names> -v
```

Expected: failures showing partial market persistence, evidence surviving caller rollback, future evidence visible to a later report, unrelated direct membership, and unsafe request IDs being accepted.

### Task 2: Implement the shared boundary repair

**Files:**
- Modify: `src/nisa_quant/evidence_collection.py`

**Interfaces:**
- Consumes: Existing canonical identity functions, `contains_control_content`, scope tables, bindings, and report queries.
- Produces: `_validate_request_id` rejecting unsafe identifier/directive shapes without echoing values; exact direct target-to-member resolution; market batch validation and rollback; evidence transaction ownership and cutoff checks; report cutoff clamping.

- [ ] **Step 1: Extend request-ID validation before any persistence.**
  - Retain the typed identifier grammar.
  - Apply the existing recursive control/credential scanner and a delimiter-aware request-ID shape check for token, credential, broker/account, and directive forms.
  - Keep the error generic and value-free.
  - Invoke the validator at every persistence entrypoint, including scope, failure, run, snapshot, and report paths.
- [ ] **Step 2: Add exact direct membership resolution.**
  - Resolve only active latest universe rows matching the requested ticker/CIK targets at the requested cutoff.
  - Reject ambiguous ticker-only mappings; omit unavailable targets so their existing typed failure can be bound to an exact requested scope without importing unrelated members.
  - Pass exact member IDs into implicit market and evidence scopes; leave explicit configured scope behavior unchanged.
- [ ] **Step 3: Make market response ingestion atomic.**
  - Validate every response item and derive all canonical field plans before inserting any market fact.
  - Start an outer transaction if the connection is idle, create a batch savepoint, insert facts/bindings, detect conflicts, and roll back the batch on validation or SQLite failure.
  - Record one deterministic sanitized failure after rollback, preserve `usable_result`, provider-unavailable handling, `commit=True`, caller-managed `commit=False`, replay, and connection ownership.
- [ ] **Step 4: Correct evidence caller-managed transaction ownership.**
  - Start an outer transaction before the evidence savepoint when needed, especially when an explicit pre-existing scope makes scope creation a no-op.
  - Release only the inner savepoint on success; commit only when requested.
  - Keep rollback-to-savepoint behavior for an already-owned outer transaction.
- [ ] **Step 5: Enforce immutable evidence cutoff and report clamping.**
  - Reject evidence whose normalized publication or retrieval date is after the bound scope cutoff before persistence.
  - Keep chronology/freshness/replay behavior.
  - After selecting an explicit/request-bound scope, clamp a later report request cutoff to that scope’s immutable `as_of`; preserve rejection when the requested cutoff predates the scope.

### Task 3: Verify, review, and hand off

**Files:**
- Inspect: `src/nisa_quant/evidence_collection.py`, `tests/test_phase2.py`, and the complete dirty diff.

**Interfaces:**
- Consumes: Task 1 regressions and Task 2 implementation.
- Produces: Fresh verification evidence and an uncommitted working tree with no push/commit.

- [ ] **Step 1: Run the consolidated targeted matrix.**
  - Include the five new regressions plus existing request A/B conflict and replay immutability, exact/changed/blank replay fingerprints, empty/future/inactive universe, binding migration interruption recovery, encoded scanner, DNS/rebinding, and security boundary tests.
- [ ] **Step 2: Run the required commands exactly.**

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m nisa_quant --help
git diff --check
```

- [ ] **Step 3: Inspect status and diff for scope, secret, and generated-bytecode violations.**
- [ ] **Step 4: End with `status=awaiting_verification`; do not commit or push.**
