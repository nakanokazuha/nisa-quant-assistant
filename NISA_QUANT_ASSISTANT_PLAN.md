# Historical research / deferred — not active implementation instructions

Release boundary: Implemented now: local fixture/CSV/SQLite/CLI only. Deferred roadmap: Hermes/live providers/scheduling/delivery. Prohibited: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

# NISA Quant Assistant Implementation Plan

> **Historical planning note:** This document records a proposed Hermes/Yume investment workflow. It is not an active routing instruction; no Hermes runtime, orchestrator, or model-provider routing is implemented in the current release.

**Goal:** Build a local-first, advisory-only quant assistant that turns sourced Japan/international market data and broker CSV exports into deterministic metrics, cited BUY/HOLD/SELL candidate reports, and recommendation outcome tracking.

**Architecture:** The implemented architecture is a local Python CLI with fixture/CSV ingestion, SQLite, deterministic calculations, screens, cited Markdown, and journal evaluation. Hermes narrative routing, live adapters, and delivery were historical future proposals.

**Tech Stack:** Implemented: Python, SQLite, CSV fixtures, Markdown, and the local CLI. Historical deferred proposals included Hermes/cron, J-Quants V2, yfinance, issuer/JPX/EDINET/TDnet sources, optional analytics libraries, and Discord delivery; none are active dependencies or routing instructions.

**Spec:** `/Users/user/Documents/Yume/nisa-research/NISA_QUANT_ASSISTANT_SPEC.md`  
**Project directory:** `/Users/user/Documents/Yume/nisa-research/`  
**Pre-development rule:** Keep artifacts and implementation local/uncommitted until the pre-development baseline is complete and Ilham explicitly authorizes implementation.

## Current release boundary

Implemented now: local fixture/CSV/SQLite/CLI only.

Deferred/not implemented: Hermes runtime integration, live providers, scheduling, and delivery.

Prohibited boundaries: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

The gates and tasks below are historical roadmap material unless they describe the implemented local slice. They cannot be read as active routing instructions or authorization to add deferred integrations.

---

## 0. Historical governance and future implementation gates

### Gate G0 — Spec approval

**Entry:** This plan and the standalone specification exist.  
**Exit:** Ilham explicitly approves the spec/plan for implementation. “LGTM” on the research proposal is not automatically approval of every implementation detail.

### Gate G1 — Source and data contract (historical live-provider gate; deferred)

**Exit evidence:** J-Quants live plan/endpoint entitlement checked; first broker CSV format obtained in redacted form; selected issuer/filing sources documented; identifiers and timestamp policy fixed.

### Gate G2 — Local fixture slice

**Exit evidence:** Redacted fixtures import into SQLite, deterministic calculations pass, and safe labels appear without any real credential or live broker login.

### Gate G3 — Local report slice (Hermes integration deferred)

**Exit evidence:** The local implementation produces a cited Markdown report from a fixed snapshot; numeric facts are sourced/computed; missing/stale/conflicting data produces safe output. Hermes/Codex runtime generation is deferred.

### Gate G4 — Recommendation journal/evaluation

**Exit evidence:** Recommendations can be stored with cutoff/model/template/source metadata and evaluated later against a passive benchmark without look-ahead.

### Gate G5 — Real-data opt-in (deferred/not implemented)

**Exit:** Only after privacy/retention/redaction review and Ilham's explicit approval to import real personal CSVs or configure credentials. No broker write credential is ever accepted.

### Gate G6 — Recurring delivery (deferred/not implemented)

**Exit evidence:** A scheduled weekly/monthly job can produce and deliver a redacted report; failed, stale, or unavailable inputs are visible and do not create fabricated continuity.

---

## 1. Repository and data layout

**Objective:** Establish a standalone project layout that cannot be confused with Yume iOS.

**Proposed paths:**

```text
/Users/user/Documents/Yume/nisa-research/
├── NISA_QUANT_ASSISTANT_SPEC.md
├── NISA_QUANT_ASSISTANT_PLAN.md
├── README.md
├── pyproject.toml                       # only if a package is justified
├── src/nisa_quant/
│   ├── schema.py
│   ├── imports.py
│   ├── sources.py
│   ├── metrics.py
│   ├── screens.py
│   ├── recommendations.py
│   ├── reports.py
│   └── journal.py
├── tests/fixtures/
├── tests/
├── data/                                # local-only, gitignored
└── reports/                             # generated, retention-controlled
```

**Rules:**

- Never place raw broker exports, API keys, account numbers, or generated personal reports in source control.
- Add `.gitignore` before any real data is imported.
- Do not create files under `/Users/user/Documents/Yume/yume-ios`.
- Avoid adding a dependency until the standard library/SQLite implementation is demonstrably insufficient.

---

## 2. Source and identifier contract (local records implemented; live providers deferred)

**Objective:** Make every fact traceable before writing a screen or report.

### Tasks

1. Create a source registry schema with provider, URL/API, retrieval timestamp, observation date, instrument identifier, field, value, unit, currency, freshness status, parser version, and citation location.
2. Record J-Quants V2 as the Japan-first source and verify its current Free-plan delay, history, rate limit, and fields immediately before implementation.
3. Record yfinance as an unofficial supplementary source with `.T` identifier handling and explicit coverage caveat.
4. Record issuer pages for held ETFs and official JPX/EDINET/TDnet sources for filings and distributions.
5. Define canonical instrument identifiers without silently conflating JPX codes, Yahoo symbols, ISINs, fund names, or foreign listings.
6. Define the source precedence and conflict policy: primary official source, structured provider, secondary corroboration, then unavailable/manual review.

### Acceptance tests

- A source record can explain where a sample price, distribution, and filing fact came from.
- A stale or conflicting source is retained as a warning, not silently overwritten.
- Every identifier conversion is explicit and test-covered.

---

## 3. SQLite ledger and redacted fixture importer

**Objective:** Normalize a broker export without automatic account access.

### Tasks

1. Create tables for `instruments`, `accounts`, `transactions`, `cash_movements`, `positions`, `source_records`, `imports`, and `data_warnings`.
2. Define account types for NISA, taxable 特定口座, general, cash, foreign currency, and unknown/review.
3. Implement a CSV column-mapping layer that rejects ambiguous or missing required columns instead of guessing.
4. Add idempotent import identity based on file hash plus source metadata, without storing the raw file in the repository.
5. Normalize Japanese dates, decimal values, currency, security code, quantity, price, fee, distribution, and account labels.
6. Preserve import warnings and the original source filename only if that filename contains no account secret or personal identifier.

### TDD/verification

- Fixture: one NISA purchase, one taxable holding, one cash movement, one distribution, one malformed row, one unknown account label.
- Verify repeated import does not duplicate accepted transactions.
- Verify malformed/ambiguous rows are quarantined for review.
- Verify NISA and taxable positions cannot merge during aggregation.
- Verify raw fixture contains synthetic/redacted values only.

---

## 4. Deterministic calculations

**Objective:** Compute portfolio facts independently of the language model.

### Tasks

1. Implement market value using an explicitly dated source price and currency.
2. Implement contributions, cash movements, cost basis, realized/unrealized P/L where inputs permit.
3. Implement allocation by account, asset, currency, geography, and sector only when metadata exists.
4. Implement return series, drawdown, volatility, concentration, distributions, and benchmark-relative performance.
5. Implement data-quality status for missing price, delayed price, stale source, unsupported currency, unresolved identifier, and incomplete transaction history.
6. Document formulas and edge cases: split-adjusted series, distributions, fees, FX, partial history, and zero/negative quantities.

### Acceptance tests

- Expected values are checked with fixed fixtures using exact tolerances documented in tests.
- No calculation calls the LLM.
- No missing input is replaced with zero unless the source contract explicitly says zero.
- A report can show both price return and distribution-inclusive return where supported.

---

## 5. Candidate screens and recommendation policy (local safe screens implemented; advanced narrative deferred)

**Objective:** Turn deterministic evidence into ranked candidates without pretending to predict certainty.

### Initial screens

1. Trend/momentum and moving-average context.
2. Drawdown/crash-guard context.
3. Valuation and quality fields when source-backed.
4. Volatility and concentration.
5. Benchmark-relative behavior.
6. ETF distribution/composition changes when issuer data supports them.

### Tasks

1. Define each screen's input fields, formula, lookback, missing-data behavior, and interpretation.
2. Produce a score/explanation object, not a direct trade command.
3. Combine screen outputs into ranked candidates using transparent weights or a rule table.
4. Require evidence quality, horizon, counter-evidence, risk, invalidation condition, and “what changes the view.”
5. Map output to `BUY CANDIDATE`, `HOLD`, `SELL / REDUCE CANDIDATE`, `WATCH`, or `NO ACTION / INSUFFICIENT DATA`.
6. Add a hard stop: material missing, stale, contradictory, or unsupported data prevents a directional label.

### Acceptance tests

- Fixture with strong trend produces a candidate with the formula and source attached.
- Fixture with stale price produces `WATCH` or `NO ACTION / INSUFFICIENT DATA`.
- Fixture with conflicting valuation values produces a warning and no overconfident directional label.
- Output contains no broker/order API fields and no executable action.

---

## 6. Cited Hermes/Codex-Luna report generation (deferred/not implemented)

**Objective:** Let Hermes explain sourced evidence while deterministic components retain numeric authority.

### Report sections

1. Report time and all data cutoffs.
2. Portfolio/account changes and data warnings.
3. Market context and benchmark state.
4. Ranked recommendation table.
5. Per-candidate evidence, counter-evidence, risk, horizon, and invalidation.
6. Distribution/earnings reminders.
7. “Manual review required; no order was placed” statement.
8. Source list with URLs and retrieval dates.

### Tasks

1. Create a structured report input JSON/SQLite view containing only approved facts and citations.
2. Define a concise prompt/template for the approved Codex Luna-backed Hermes workflow that forbids inventing numeric facts and requires citation IDs.
3. Validate the generated Markdown against required sections, label vocabulary, citations, cutoff timestamps, and redaction rules.
4. If the model returns unsupported facts, replace the section with a data warning rather than repairing it by guesswork.
5. Record actual model/provider/template version with each report.

### Acceptance tests

- Same input snapshot yields identical numeric tables across runs even if prose varies.
- Unsupported model claims fail validation.
- Redaction scan rejects API keys, account identifiers, raw CSV content, and secrets.
- Report remains useful when one provider is unavailable.

---

## 7. Recommendation journal and outcome evaluation

**Objective:** Learn whether recommendations were useful without hindsight contamination.

### Tasks

1. Store recommendation ID, created time, market/data cutoff, model ID, template version, source IDs, label, metrics, thesis, risks, and horizon.
2. At later evaluation dates, append observed outcome and passive benchmark values using only data available by that date.
3. Report hit rate only with a clear definition; also report benchmark-relative return, drawdown, turnover hypothetical, fees, and missed/insufficient-data cases.
4. Keep recommendation history separate from current portfolio accounting.
5. Never rewrite the original recommendation after new information arrives; append a correction or follow-up.

### Acceptance tests

- Historical evaluation cannot read future source records when building an earlier report.
- A recommendation can be reproduced from its original snapshot.
- A passive benchmark is always shown where comparison is meaningful.
- No result is summarized as guaranteed alpha.

---

## 8. ETF distributions, filings, and event monitor

**Objective:** Add high-value, low-overtrading monitoring for held ETFs and stocks.

### Tasks

1. Create an issuer watchlist for Ilham-approved instruments, initially examples such as 1321 and 2559 only after confirmation.
2. Parse or manually record issuer distribution announcements, ex-dates, payment dates, and per-unit amount with citations.
3. Add J-Quants earnings-date and financial-summary ingestion where the selected plan supports it.
4. Add official EDINET/TDnet/issuer filing links to deep-research memos.
5. Treat parser ambiguity as a human review item.

### Acceptance tests

- A synthetic distribution notice produces a reminder with issuer citation.
- Missing or changed distribution data produces a warning, not an inferred amount.
- Events are not turned into automatic BUY/SELL actions.

---

## 9. Hermes skill and recurring jobs (deferred/not implemented)

**Objective:** Make the workflow callable from Hermes without creating a separate mobile application.

### Tasks

1. Package the approved workflow as a standalone Hermes skill or local tool only after the local fixture slice passes.
2. Add a manual command for on-demand deep research.
3. Add a weekly brief job only after report validation and redaction tests pass.
4. Add a monthly portfolio report job only after the ledger has an approved input snapshot.
5. Configure delivery to Discord only with minimal necessary personal detail and explicit source/cutoff wording.
6. Ensure job failure, stale data, API rate limiting, and missing CSV input generate an actionable error report.

### Operational rules

- No automatic broker login.
- No automatic order execution.
- No background claim beyond what Hermes scheduling actually proves.
- No silent fallback to an uncited or paid source.
- No raw portfolio data in public channels.

### Acceptance tests

- Manual report works with fixture data.
- Scheduled report either delivers a validated report or a clear failure/warning.
- A missing input does not produce a plausible-looking fabricated report.
- Job output states the model ID and data cutoff without exposing secrets.

---

## 10. Optional integrations, deferred

These are not first-slice dependencies:

- `jquants-mcp` for direct MCP access to J-Quants.
- `edinet-db-mcp` for Japanese filing research.
- TradingAgents for occasional structured second opinions on `.T` tickers.
- QuantStats for portfolio tear sheets.
- vectorbt for transparent strategy sanity checks.
- Monex MCP Server if Ilham uses Monex and its read/privacy scope is independently verified.

Each optional component requires a separate source/license/dependency/security review. None may introduce broker write access, automatic dispatch, or a new unapproved authority model.

---

## 11. Verification matrix (historical target; current release uses local checks only)

| Area | Evidence required | Pass condition |
|---|---|---|
| Data ingestion | Fixture + source metadata | Facts have source/date/freshness |
| CSV import | Redacted broker fixture | No guessing; account separation preserved |
| Accounting | Deterministic test vectors | Numeric outputs match documented formulas |
| Screens | Synthetic scenarios | Ranking and safe fallback are explainable |
| Report | Fixed snapshot + Codex Luna-backed output | Required sections/citations/redaction pass |
| Journal | Original snapshot + later snapshot | No look-ahead; benchmark included |
| Privacy | Secret/PII scan | No credentials/raw personal data escape |
| Resilience | Provider outage/stale/malformed fixtures | Explicit warning or safe no-action result |
| Scheduling | Controlled local job run | Valid report or actionable failure |
| Boundary | Repository/source scan | No order execution and no yume-ios changes |

---

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Free data is delayed or rate-limited | Show freshness; use weekly/monthly cadence; recheck plan limits |
| yfinance coverage/terms change | Use as supplementary source; prefer JPX/J-Quants for Japan |
| Broker CSV schema changes | Version mappings; reject ambiguous rows; retain import warnings |
| LLM hallucinates financial numbers | Deterministic input envelope, citation validator, no unsupported values |
| LLM timing underperforms passive investing | Benchmark every recommendation; present counter-evidence; no alpha claim |
| NISA accounting is mixed with taxable account | Account dimension is mandatory in every transaction/position table |
| Sensitive data leaks into Discord/logs | Local-first ledger, redaction validator, minimum report detail |
| Heavy framework becomes unmaintainable | Keep core stack small; optional frameworks remain deferred |
| Model/provider availability changes | Record actual model/provider; fail visibly; never silently substitute a different model |
| Scope drifts into Yume iOS or execution | Explicit project-separation scan and hard boundary requirements |

---

## 13. Historical suggested execution order (not an active routing plan)

```text
G0 spec approval
  → source/identifier contract
  → redacted CSV fixture + SQLite ledger
  → deterministic calculations
  → candidate screens
  → fixed-snapshot Codex Luna-backed report
  → recommendation journal/evaluation
  → ETF/filing monitor
  → standalone Hermes skill
  → weekly/monthly scheduling
  → optional MCP/deep-research integrations
```

Do not skip the fixture slice. Do not configure real credentials or recurring delivery before the report and redaction validators pass.

---

## 14. Historical definition of done for a future integrated v0

The standalone NISA Quant Assistant is ready for normal use only when:

- the first broker CSV is imported reliably without login automation;
- NISA/taxable/cash/FX accounting remains distinct;
- market data and every recommendation fact has source and cutoff metadata;
- deterministic calculations and screens pass fixture tests;
- Codex Luna-backed Hermes workflow produces a cited report without inventing numbers;
- recommendations include BUY/HOLD/SELL candidate labels plus risks and uncertainty;
- recommendation outcomes can be evaluated against a passive benchmark;
- stale/missing/conflicting data produces safe no-action output;
- secrets and raw personal data remain out of source, prompts, logs, and public delivery;
- no order endpoint, broker write credential, or automatic dispatch exists;
- the project remains separate from Yume iOS;
- Ilham reviews and explicitly approves the completed pre-development baseline before production implementation is considered authorized.
