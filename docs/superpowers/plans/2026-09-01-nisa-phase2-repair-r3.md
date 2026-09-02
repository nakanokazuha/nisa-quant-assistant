# NISA Phase 2 Repair R3 Implementation Plan

Historical research / deferred — not active implementation instructions
Release boundary: local Phase 2 security and integrity repair only; no external execution.
Implemented now: local read-only evidence fixtures, configured provider guards, and CLI tests only.
Deferred/not implemented: Phase 3 prediction/verdict work, Hermes/MCP, scheduling, broker access, and trading.
Prohibited: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

> **For agentic workers:** This repair is executed inline in the current dirty checkout; do not commit or push. Steps use checkbox syntax for tracking.

**Goal:** Repair the existing Phase 2 read-only evidence layer so provider trust, content integrity, chronology, cutoff/run scope, SEC pacing, freshness, failure status, and legacy CLI compatibility are fail-closed and replayable.

**Architecture:** Preserve the existing SQLite tables and provider-neutral adapters. Add shared validation at provider, record, persistence, and report boundaries; pass explicit run context and safe event timestamps through refresh orchestration; keep configured providers separate from legacy recommendation commands.

**Tech Stack:** Python 3.11 standard library, SQLite, `urllib`, `ipaddress`, `time`, `unittest`; no new dependency.

**Spec:** This task contract and `docs/superpowers/specs/2026-09-01-nisa-phase2-r1-design.md`.

## Global Constraints

- Repair only the existing Phase 2 US-listed S&P 500 read-only evidence layer and the documented legacy compatibility test.
- Never disclose secrets; configured provider URLs must use explicit official host allowlists, while RSS is secret-free and separately validated.
- Reject control, directional, and credential-shaped material before persistence or report emission.
- Preserve atomic/replayable failure auditing, provenance, chronology, conflict handling, and the no-verdict/no-order boundary.
- Do not start Phase 3, Hermes/MCP, scheduling, broker access, or trading.
- Do not commit or push.

### Task 1: Add red regression tests

**Files:**
- Modify: `tests/test_phase2.py`
- Modify: `tests/test_cli.py`

- [x] Add focused tests for malicious configured host secret exfiltration, private/IP host rejection, all EvidenceRecord fields and emitted collections content scanning, malformed market timestamps, Company Facts top-level CIK mismatch and absent-accession policy, current-run/future-universe cutoff scoping, SEC limiter configuration, impossible freshness labels, configured all-provider failure status/exit, and the restored legacy fixture candidate path.
- [x] Run each new focused test before implementation and confirm the failure is caused by the missing boundary behavior.
- [x] Keep all credentials in tests redacted or supplied only in memory.

### Task 2: Repair transport trust and integrity validation

**Files:**
- Modify: `src/nisa_quant/phase2_sources.py`
- Modify: `src/nisa_quant/phase2.py`

- [x] Enforce explicit Alpha Vantage and SEC official host allowlists, reject IP/private/loopback/link-local/credentialed references and redirects, and prevent configured URL values from becoming a trust allowlist.
- [x] Add shared recursive scanning for all record fields, universe/market source fields, failure/report collections, and nested metadata; retain safe issuer/news prose.
- [x] Keep failure messages redacted and never normalize malformed event timestamps as observation timestamps.

### Task 3: Repair chronology, identity, cutoff/run scope, pacing, freshness, and status

**Files:**
- Modify: `src/nisa_quant/phase2_sources.py`
- Modify: `src/nisa_quant/phase2.py`
- Modify: `src/nisa_quant/__main__.py`
- Modify: `docs/phase2-sources.md`

- [x] Validate Company Facts payload CIK identity against the explicit request; document absent accession behavior as explicit source identity without invented accession.
- [x] Use normalized retrieval time for live configured calls, select only active/effective current-run universe members at or before `as_of`, and exclude future/unrelated rows.
- [x] Add an injectable SEC local limiter consuming `max_requests_per_second` without test delays.
- [x] Recompute/downgrade freshness from chronology so unusable/stale rows cannot be usable.
- [x] Return non-success for configured refresh unless a configured provider actually succeeds; retain deterministic failure audit.

### Task 4: Restore compatibility determinism and verify

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `tests/test_reports_journal.py` only if required by the restored workflow
- Modify: `README.md` only if the documented command needs an exact deterministic clarification

- [x] Restore the original candidate assertion and use an explicit current-date test cutoff/input workflow so it remains meaningful without depending on an obsolete wall-clock date.
- [x] Run targeted probes and the fresh temporary fixture/configured E2E with cleanup.
- [x] Run the required full unittest, compileall, CLI help, and diff-check commands.
- [x] Leave the checkout uncommitted/unpushed with final status `awaiting_verification`.
