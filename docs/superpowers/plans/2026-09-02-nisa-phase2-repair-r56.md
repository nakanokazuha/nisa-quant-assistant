# Phase 2 Conflict Isolation Repair Implementation Plan

Historical research / deferred — not active implementation instructions. This local plan records the approved Phase 2 repair workflow only.
Release boundary: local Phase 2 repair plan only.
Implemented now: local read-only SQLite/provider boundary and tests.
Deferred roadmap: live providers, scheduling, delivery, and integrations.
Prohibited: credentials, broker login/write/order execution, and Yume iOS changes.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Phase 2 market and evidence conflict detection immutable for prior request-bound rows while evaluating only the current batch’s touched identities/keys and cutoff-eligible global variants.

**Architecture:** Keep the existing append-only fact tables and request/scope binding tables. Pass the current batch’s newly bound record identities into each detector; each detector will query only those keys/identities and only facts eligible at the immutable scope cutoff, while updating only the current request/scope binding rows. Existing global fact conflict metadata remains audit information and cannot drive another request’s report.

**Tech Stack:** Python 3, stdlib `unittest`, SQLite, `sqlite3.Connection`, `apply_patch`.

**Spec:** `NISA_QUANT_ASSISTANT_SPEC.md` and the user-provided `nisa-phase2-repair-r56` contract.

## Global Constraints

- Phase 2 remains read-only evidence/data layer for US-listed S&P 500 equities only.
- No Phase 3 verdicts, broker login/write, trading/orders, live providers, credentials, scheduler, MCP, Hermes, or yume-ios changes.
- Do not commit or push; preserve the existing dirty checkout.
- Future evidence must be rejected before global persistence and cannot participate in conflicts.
- A caller cannot override an existing request scope’s immutable `as_of` cutoff.
- Preserve exact request→scope binding, direct scope isolation, replay fingerprints, failure attribution, atomic batches, caller-managed transactions, migrations, and request-id safety.

### Task 1: Add the regression family

**Files:**
- Modify: `tests/test_phase2.py`

**Interfaces:**
- Consume existing `new_connection`, `seed_universe`, `evidence_record`, `MarketObservation`, `_persist_refresh_scope`, `refresh_market_observations`, `ingest_evidence`, and `phase2_evidence_report` helpers.
- Produce a test that records prior reports and binding/failure state, executes A usable → B conflicting → A unrelated continuation for market and evidence, and verifies replay, valid controls, future-evidence rejection, direct request isolation, atomicity, and request-id safety.

- [x] **Step 1: Write the failing regression test**

  Add one test method that uses two typed universe members, immutable scopes, one shared market key/evidence identity, and one unrelated ticker/identity. Assert that after request B introduces an eligible conflicting variant, request A’s first report and original binding/failure state are unchanged after A ingests the unrelated continuation. Assert B sees the conflict, replay does not duplicate facts/failures, an exact-value control remains usable, and a future evidence record is rejected before persistence. Include direct scope mismatch, invalid request-id, atomic invalid batch, and a query/assertion proving only current-batch keys were evaluated.

- [x] **Step 2: Run the new test to verify the pre-fix failure**

  Run `python3 -m unittest tests.test_phase2.Phase2ReviewRegressionTests -v` after identifying the existing review regression class, or the exact new test method with `python3 -m unittest tests.test_phase2.<class>.<method> -v`.

  Expected: the new A→B→A continuation assertion fails because the later conflict call re-evaluates A’s historical shared binding and moves it to `conflict`.

### Task 2: Implement batch-local conflict detection

**Files:**
- Modify: `src/nisa_quant/phase2.py`

**Interfaces:**
- Consume the current batch’s inserted/replayed market observation IDs and evidence IDs at the existing persistence boundary.
- Produce detector behavior that evaluates only current-batch touched keys/identities, filters all global variants by observation/retrieval or publication/retrieval cutoff, and updates only current request/scope binding status.

- [x] **Step 1: Pass current-batch IDs into market conflict detection**

  Replace the request-wide touched-key/identity discovery with the newly bound IDs collected while persisting this batch. Keep the detector’s explicit scope/request validation and preserve idempotent replay behavior.

- [x] **Step 2: Pass current-batch IDs into evidence conflict detection**

  Apply the same boundary to evidence. Retain the pre-persistence `_EvidenceAfterScopeCutoff` rejection and use the scope cutoff when selecting global variants.

- [x] **Step 3: Reject scope-cutoff overrides before persistence**

  Validate any explicit `as_of` against the existing request-bound scope cutoff for both market and evidence operations; reject mismatches before provider calls or fact-table writes.

- [x] **Step 4: Run the regression test and focused existing conflict tests**

  Run the new method plus existing `test_cross_request_conflicts_are_bound_to_the_request_and_do_not_rewrite_prior_reports`, `test_future_evidence_cannot_create_cross_request_identity_conflict`, `test_replaying_conflicted_request_is_deterministic_without_duplicate_failures`, and `test_request_bound_replay_stays_usable_after_market_and_evidence_conflicts`.

### Task 3: Full verification and handoff

**Files:**
- No additional implementation files.

- [x] **Step 1: Run targeted in-memory probes**

  Exercise market and evidence A→B→A continuation, future evidence isolation, direct request isolation, replay, atomicity, and request-id safety against real in-memory SQLite connections. Capture exact outcomes without secrets or personal data.

- [x] **Step 2: Run the required commands**

  Run exactly:

  ```text
  python3 -m unittest discover -s tests -v
  python3 -m unittest tests.test_phase2.Phase2ReviewRegressionTests -v
  python3 -m compileall -q src tests
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m nisa_quant --help
  git diff --check
  ```

- [x] **Step 3: Inspect the final diff and hand off**

  Confirm only the requested source/test changes plus the local plan are present, do not stage/commit/push, and finish the response with `status=awaiting_verification`.
