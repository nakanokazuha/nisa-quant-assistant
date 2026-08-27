# NISA Quant Assistant — Product Specification

**Status:** Draft for Ilham review  
**Project boundary:** Standalone Hermes/Yume investment assistant  
**Relationship to Yume iOS:** None. This specification does not modify, extend, or authorize work in `/Users/user/Documents/Yume/yume-ios`.  
**Implementation:** Production implementation must be delegated through `codex-orchestrator`; the executor uses `gpt-5.6-luna` with high reasoning and the reviewer uses `gpt-5.6-sol` with medium reasoning.  
**Date:** 2026-08-24 (Asia/Tokyo)

---

## 1. Product definition

NISA Quant Assistant is a personal investment-research system operated through Hermes. It acts like a careful quant/research analyst: it gathers evidence, computes portfolio and market metrics, produces ranked candidates, explains risks, tracks prior recommendations, and gives Ilham a decision-ready report.

It is **not** an automated trading system, broker robot, order router, or replacement for Ilham's investment judgment.

### Primary output

The assistant may provide explicit recommendation labels:

- `BUY CANDIDATE` — evidence supports considering a new/additional purchase;
- `HOLD` — no evidence-backed reason to change the position;
- `SELL / REDUCE CANDIDATE` — evidence supports considering reduction or exit;
- `WATCH` — insufficient evidence or an event is worth monitoring;
- `NO ACTION / INSUFFICIENT DATA` — the correct answer when inputs are incomplete, stale, or contradictory.

These are recommendations for **manual human review**. They are not certainty claims, financial guarantees, or executable instructions.

---

## 2. Goals and non-goals

### Goals

1. Help Ilham research Japanese and international stocks and ETFs suitable for a Japan-based NISA investor.
2. Produce recurring weekly/monthly market briefs with citations and data timestamps.
3. Generate watchlist screens using deterministic metrics such as momentum, valuation, drawdown, volatility, quality, distributions, and benchmark-relative performance.
4. Maintain a deterministic portfolio ledger that separates NISA, taxable, cash, and foreign-currency positions.
5. Provide company/ETF deep dives from official filings, issuer pages, JPX/J-Quants data, and other clearly identified sources.
6. Track recommendations over time and compare them with passive benchmarks without hindsight contamination.
7. Make uncertainty, missing data, stale data, conflicting sources, and model limitations visible.
8. Use Hermes with the approved OpenAI Codex `gpt-5.6-luna` model at high reasoning for analysis, summaries, images, and files; record the actual model/provider per report and never silently substitute another model. The model narrates and reasons over sourced inputs but does not become the accounting authority.

### Non-goals

- No automatic order execution, order submission, trading API write access, portfolio mutation, or broker control.
- No automatic login or scraping of broker credentials.
- No tax-loss harvesting workflow for NISA; NISA losses cannot offset other gains or be carried forward.
- No claim that an LLM signal has alpha or will outperform buy-and-hold.
- No high-frequency, intraday, or latency-sensitive trading system.
- No autonomous portfolio rebalancing or automatic BUY/SELL dispatch.
- No Yume iOS feature, iOS UI, iOS transport, HealthKit integration, or Hermes mobile gateway change.
- No invented financial data. If a number cannot be sourced or computed reproducibly, it is omitted or marked unavailable.

---

## 3. Users and usage cadence

**User:** Ilham, single-user personal investor in Japan.  
**Timezone:** Asia/Tokyo.  
**Investment style assumed:** long-term holdings, monthly accumulation, occasional additions/reductions, stocks and ETFs, NISA priority.

### Initial cadence

| Job | Cadence | Purpose |
|---|---:|---|
| Watchlist/market brief | Weekly | Context, material changes, candidates, risks |
| Portfolio reconciliation | After each broker CSV export and monthly | Holdings, cost basis, contribution and allocation truth |
| Recommendation review | Weekly or event-triggered | Ranked BUY/HOLD/SELL candidates with evidence |
| Portfolio/risk report | Monthly | Returns, drawdown, benchmark, concentration, drift |
| Deep research | On demand | Company/ETF thesis and source-backed investigation |

The cadence must not be described as a latency guarantee. Jobs may be delayed, skipped, or report stale/unavailable status.

---

## 4. Proposed system boundary

```text
Official/public data + broker CSV exports
              │
              ▼
      Local source-normalized data
      (SQLite or equivalent ledger)
              │
              ├── deterministic calculations
              │   (returns, risk, valuation deltas, screens)
              │
              ├── source/citation records
              │
              └── recommendation history + benchmark outcomes
                              │
                              ▼
                  Hermes + OpenAI Codex gpt-5.6-luna
                  (explanation and synthesis)
                              │
                              ▼
              Markdown / Discord report for Ilham
                              │
                              ▼
                  Ilham decides and trades manually
```

### Authority rules

- Source providers are authoritative for the facts they publish.
- The local ledger is authoritative only for the normalized records it has accepted; it is not a broker or host source of truth.
- Deterministic code is authoritative for derived arithmetic and portfolio accounting.
- Hermes with OpenAI Codex Luna is responsible for synthesis, prioritization, explanation, and uncertainty wording.
- Ilham alone authorizes and performs any investment transaction.

No component may silently convert a stale cache, model inference, or missing value into a current fact.

---

## 5. Functional requirements

### FR-01 — Watchlist

The system shall maintain a user-editable watchlist of security identifiers, display names, asset type, market/exchange, currency, benchmark, and notes. Japanese identifiers may use JPX codes and Yahoo Finance `.T` symbols where applicable.

### FR-02 — Market data ingestion

The system shall support a source-backed ingestion path for daily prices and relevant metadata, prioritizing J-Quants V2 for Japanese-listed instruments and yfinance where appropriate. Each record shall retain source, retrieval time, observation date, and freshness/availability status.

The first implementation shall not assume that a free source provides real-time data. J-Quants Free's reported 12-week delay and rate limits must be visible in the data status.

### FR-03 — Portfolio import

The system shall accept broker-exported CSV files as the initial portfolio input. The importer shall normalize security code, quantity, transaction date, transaction type, price, fees, currency, account classification, cash, distribution, and source file metadata without silently guessing ambiguous columns.

### FR-04 — Account separation

The ledger shall distinguish at minimum:

- NISA holdings and NISA cash;
- taxable 特定口座 holdings and cash;
- general-account holdings and cash, if used;
- foreign-currency balances;
- unknown/unmapped account rows requiring review.

### FR-05 — Deterministic portfolio calculations

The system shall compute, from normalized records and cited prices:

- market value and unrealized/realized P/L where inputs permit;
- contributions and cash movements;
- allocation by account, asset, currency, sector, and geography where source data supports it;
- drawdown, volatility, and concentration;
- distributions/dividends recorded separately from price return;
- benchmark-relative performance;
- data-quality warnings.

All formulas, price dates, currency assumptions, and missing-input behavior shall be documented and testable.

### FR-06 — Deterministic candidate screens

The system shall support transparent screens, initially:

- trend/momentum and moving-average context;
- drawdown/crash-guard context;
- valuation and financial-quality fields when sourced;
- volatility and concentration;
- distribution/yield changes for ETFs where issuer or official data supports them;
- benchmark-relative behavior.

A screen produces candidates and reasons; it does not place an order or claim prediction certainty.

### FR-07 — Research brief

Hermes shall produce a cited Markdown brief containing:

1. report timestamp and data cutoffs;
2. market context;
3. portfolio changes and data warnings;
4. ranked recommendation table;
5. evidence and counter-evidence per candidate;
6. key risks and invalidation conditions;
7. action vocabulary and manual-review reminder;
8. sources with URLs and retrieval dates.

### FR-08 — Recommendation labels and rationale

Every recommendation shall include:

- instrument and account context, if relevant;
- one of `BUY CANDIDATE`, `HOLD`, `SELL / REDUCE CANDIDATE`, `WATCH`, or `NO ACTION / INSUFFICIENT DATA`;
- thesis/reason;
- deterministic metrics used;
- source citations and dates;
- confidence expressed as evidence quality, not probability of profit;
- time horizon;
- risks, counter-evidence, and invalidation conditions;
- what would change the recommendation;
- explicit statement that Ilham must decide manually.

### FR-09 — Recommendation journal

The system shall record recommendation timestamp, input cutoff, model identifier, prompt/template version, source references, label, thesis, metrics, and later outcome snapshots. Outcome tracking shall use only information available after the recommendation cutoff and shall retain a passive benchmark comparison.

### FR-10 — ETF and earnings monitoring

The system shall support watchlists for ETF issuer distributions, ex-dates/payment dates, holdings/composition changes where available, earnings dates, and material official filings. It shall prefer issuer/JPX/EDINET/J-Quants sources and mark parser uncertainty for human review.

### FR-11 — Deep research

On demand, Hermes shall assemble a source-backed company/ETF memo covering business/fund objective, valuation inputs, financial trend, distribution policy, risks, catalysts, counterarguments, and source freshness. Numeric facts must originate from structured data or cited primary documents.

### FR-12 — Manual-only action boundary

The output may say that an instrument is a BUY/SELL candidate, but the system shall not expose or invoke an order endpoint, broker write credential, click automation, order form, or automatic dispatch path.

### FR-13 — Missing/stale/conflicting data

The assistant shall refuse to produce a directional recommendation when a material required input is missing, stale beyond the configured report policy, or contradictory without resolution. It shall use `NO ACTION / INSUFFICIENT DATA` or `WATCH` instead.

### FR-14 — Reproducible reports

A report shall be reproducible from a recorded input snapshot, source metadata, deterministic calculation version, and model/template identifier. Re-running may produce different prose, but numeric inputs and recommendation evidence must remain inspectable.

---

## 6. Non-functional requirements

### NFR-01 Truthfulness

Never invent prices, holdings, quantities, P/L, financial ratios, dividend dates, or source claims. Distinguish observed, calculated, inferred, unavailable, stale, and uncertain values.

### NFR-02 Auditability

Every displayed number and recommendation reason shall map to a source record or deterministic formula. Reports shall be retained only under the selected local retention policy.

### NFR-03 Privacy and secrets

Broker credentials, API keys, account numbers, raw exports, and personal financial data shall not be placed in prompts, public reports, Discord messages, source control, or ordinary logs. Secrets remain outside the repository and use the least-privilege/read-only path.

### NFR-04 Safety

The system shall remain advisory. There shall be no path from model output to transaction execution. Any future proposal to add execution is a scope change requiring a separate security and product decision.

### NFR-05 Data freshness

Each source and report shall expose retrieval/observation timestamps and plan limitations. Freshness shall be source-specific; no global “live” claim is allowed without evidence.

### NFR-06 Evaluation

Historical recommendation outcomes shall be evaluated with timestamp discipline, transaction-cost assumptions where relevant, passive benchmarks, and no look-ahead leakage. One successful recommendation is not validation.

### NFR-07 Resilience

Rate limits, provider outages, parse failures, partial imports, and stale data shall produce explicit warnings and safe degraded reports rather than fabricated continuity.

### NFR-08 Maintainability

The first stack should remain small: Python scripts/skills, SQLite, Markdown, J-Quants/yfinance, and QuantStats or equivalent. Heavy multi-agent trading frameworks are optional research references, not core dependencies.

---

## 7. Recommendation policy

The assistant should present a ranked list, not a single magical pick. A recommended report shape is:

| Rank | Instrument | Label | Evidence quality | Horizon | Why it appears | Main risk | What changes the view |
|---:|---|---|---|---|---|---|---|
| 1 | … | BUY CANDIDATE | High/Medium/Low | Long-term | … | … | … |

### Minimum evidence standard

- At least one structured price/portfolio source for numeric market facts.
- Primary issuer/JPX/EDINET source for material company, ETF, distribution, or filing claims where available.
- At least one counterargument or risk.
- Explicit cutoff date and freshness warning.
- No label stronger than the evidence supports.

### Recommendation semantics

- `BUY CANDIDATE` means “worth considering for manual review,” not “buy immediately.”
- `SELL / REDUCE CANDIDATE` means “worth considering a reduction or exit,” not a tax-loss instruction.
- `HOLD` means no evidence-backed change under the selected horizon, not a guarantee.
- `WATCH` means the thesis is incomplete or an event/data update matters.
- `NO ACTION / INSUFFICIENT DATA` is a successful safe outcome, not a failure.

---

## 8. Data-source policy

### Initial priority

1. JPX/J-Quants V2 for Japan-listed prices, metadata, financial summaries, and earnings dates.
2. Issuer pages and official filings for ETF distributions, fund composition, and material disclosures.
3. EDINET/TDnet or an evidence-preserving connector for Japanese filings.
4. yfinance `.T` tickers for broad historical market context, with unofficial-source and coverage caveats.
5. Broker CSV exports for personal holdings and transactions.
6. Secondary news/RSS only as corroborating context, never as sole authority for an important numeric claim.

### Source record fields

At minimum: source name, URL/API identifier, retrieval timestamp, observation date, instrument identifier, field, value, unit/currency, source status, parser version, and citation location.

### No silent fallback

A fallback source may fill a gap only when its identity, coverage, freshness, and lower confidence are shown. A provider error must not be hidden by an uncited replacement value.

---

## 9. Security and privacy boundary

- No broker write API, order endpoint, order credential, browser automation, or trading action.
- Read-only data is preferred; manual CSV import is the default because it avoids account-login automation.
- Raw broker CSVs and ledger data remain local and are not pasted into public channels.
- Reports sent to Discord should contain only the minimum personal portfolio detail needed; account identifiers and secrets are always redacted.
- No raw HealthKit, iOS, Yume iOS, or device data is part of this project.
- API keys are environment/config secrets and never enter code, prompts, generated reports, or git.
- Any future integration that reads broker accounts directly requires a new threat/privacy review before use.

---

## 10. Acceptance criteria for the first usable slice

The first slice is accepted only when all are true:

1. A sample, redacted broker CSV can be imported into a local ledger without guessing ambiguous fields.
2. NISA and non-NISA account rows remain distinct.
3. A watchlist containing at least one Japanese ETF, one international ETF representation, and one Japanese stock can be normalized.
4. Price and source timestamps are shown; delayed/free-source status is visible.
5. At least one deterministic screen produces a ranked candidate list from fixture data.
6. A generated brief includes BUY CANDIDATE/HOLD/SELL or safe insufficient-data labels, reasons, counter-risks, citations, and manual-review wording.
7. Numeric results are reproducible from fixtures and formulas.
8. Recommendation history stores cutoff/model/template/source metadata and can compare against a passive benchmark.
9. Stale, missing, malformed, conflicting, and unavailable inputs produce safe warnings or `NO ACTION / INSUFFICIENT DATA`.
10. Repository/source scans confirm there is no order execution, broker write credential, automatic dispatch, or hidden integration with `yume-ios`.

---

## 11. Open decisions before implementation

| ID | Decision | Current recommendation |
|---|---|---|
| OD-01 | Which broker's CSV format is first? | Start with the broker Ilham actually uses; do not build multi-broker abstractions first. |
| OD-02 | J-Quants plan | Start Free for weekly/monthly research; verify live entitlements immediately before implementation. |
| OD-03 | Ledger storage | Local SQLite with append-only source/import metadata and deterministic normalized tables. |
| OD-04 | Report delivery | Local Markdown plus Discord delivery only after redaction review. |
| OD-05 | Initial universe | Ilham-approved watchlist; seed examples must be confirmed before production use. |
| OD-06 | Benchmark | Choose benchmark per instrument/portfolio (e.g., TOPIX, Nikkei 225, MSCI ACWI, or held ETF), recorded with rationale. |
| OD-07 | Historical evaluation horizon | Select a date range long enough to include bull, bear, and sideways regimes; no single-period cherry-pick. |
| OD-08 | Retention | Define local ledger/report/recommendation retention and deletion before importing real personal exports. |
| OD-09 | Language | Default report language Indonesian; preserve Japanese instrument/source names and labels. |
| OD-10 | Model/provider policy | Use OpenAI Codex `gpt-5.6-luna` at high reasoning for the Hermes workflow; record the actual model/provider per report and prohibit silent substitution. |

---

## 12. Explicit project separation

This project is standalone. It does not:

- add investment features to Yume iOS;
- alter `/Users/user/Documents/Yume/yume-ios`;
- reuse Yume iOS transport, Swift code, HealthKit, session, or mobile architecture;
- create a mobile investment UI;
- change the Yume iOS roadmap or FDS records.

The Hermes integration is a separate skill/tool/reporting workflow on the Mac mini.
