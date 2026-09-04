# Public-Reference Fixture Generation Implementation Plan

Historical research / deferred — not active implementation instructions

Release boundary: this local plan covers only deterministic Phase 2 fixture and documentation updates.
Implemented now: local fixture/CSV/SQLite/CLI only.
Deferred roadmap: Hermes/live providers/scheduling/delivery.
Prohibited: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the normal Phase 2 demo and acceptance fixture path with a small, deterministic AAPL/MSFT public-reference subset while preserving synthetic adversarial fixtures and production provider behavior.

**Architecture:** Keep the existing responsibility-based Python modules and fixture provider unchanged. Store the reference subset as small CSV/JSON payloads under `tests/fixtures/`, use the checked-in files in normal fixture-backed tests and examples, and document source/provenance/caveats under `docs/`.

**Tech Stack:** Python 3 standard library, `unittest`, SQLite fixture refresh APIs, CSV/JSON/XML fixtures, Markdown documentation.

**Spec:** `nisa-reference-fixtures-r64` task contract supplied in the user request.

## Global Constraints

- CSV remains only a local test/fixture format; production retrieval remains automatic through configured API/RSS adapters.
- Use only small, public, non-secret reference slices; never use broker data, credentials, private URLs, paid/paywalled content, or raw personal data.
- Do not modify automatic provider adapters, security boundaries, database schema, Phase 2 semantics, module names/layout, or `/Users/user/Documents/Yume/yume-ios`.
- Do not add network calls to normal unit tests; do not bypass bot protection, WAF, terms, or rate limits.
- Keep the reference universe explicitly limited to AAPL/MSFT and label it `current_snapshot_only` with survivorship risk disclosed.
- Do not commit or push; leave the finished diff for supervisor verification.

### Task 1: Add public-reference fixture payloads

**Files:**
- Create: `tests/fixtures/phase2_reference_universe.csv`
- Create: `tests/fixtures/phase2_reference_market.csv`
- Create: `tests/fixtures/phase2_reference_sec_aapl_submissions.json`
- Create: `tests/fixtures/phase2_reference_sec_aapl_companyfacts.json`
- Create: `tests/fixtures/phase2_reference_sec_msft_submissions.json`
- Create: `tests/fixtures/phase2_reference_sec_msft_companyfacts.json`
- Create: `tests/fixtures/phase2_synthetic_news.xml`
- Delete: `tests/fixtures/phase2_universe.csv`
- Delete: `tests/fixtures/phase2_market.csv`
- Delete: `tests/fixtures/phase2_companyfacts.json`
- Delete: `tests/fixtures/phase2_sec_submissions.json`
- Delete: `tests/fixtures/phase2_news.xml`

**Interfaces:**
- Produces exact existing `UNIVERSE_COLUMNS` and `MARKET_COLUMNS` inputs for `import_sp500_universe` and `FixtureMarketProvider`.
- Produces wrapped SEC fixture objects with explicit `target`, official `source_url`, retrieval metadata, and a tiny `payload` accepted by the existing normalizers.

- [x] **Step 1: Add the universe and market reference rows.**

  Use AAPL/0000320193 and MSFT/0000789019, `effective_date=2026-09-04`, `retrieved_at=2026-09-04T00:00:00+00:00`, `lookahead_bias_status=current_snapshot_only`, and `survivorship_bias_status=survivorship_risk_disclosed`. Use Jan 2–5 2024 daily OHLCV rows and row-specific Yahoo chart endpoint citations. Record the endpoint-unavailable limitation in the source documentation rather than adding retrieval code.

- [x] **Step 2: Add tiny SEC submissions and Company Facts payloads.**

  Include one official 10-K submission and one matching `Revenues`/revenue fact per issuer, preserving accession, filing/report dates, official SEC source URLs, filing citation derivation inputs, CIK/ticker targets, and `retrieved_at=2026-09-04T00:00:00+00:00`. Do not include User-Agent text or full API dumps.

- [x] **Step 3: Add a separately named synthetic RSS parser fixture.**

  Keep it clearly synthetic and local-only, with no claim that its article is a public historical fact.

- [x] **Step 4: Validate fixture shapes offline.**

  Run a small read-only Python check that parses all CSV/JSON/XML files, asserts exact headers/targets/source hosts, and confirms no credentials, private URLs, or control content are present.

### Task 2: Wire normal Phase 2 tests to the reference subset

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `tests/test_phase2.py`

**Interfaces:**
- Consumes the reference fixture files from Task 1 through the existing CLI and normalization APIs.
- Preserves synthetic inline/helper data for malformed, conflict, security, chronology, and adapter-boundary tests.

- [x] **Step 1: Update the CLI normal fixture flow.**

  Replace normal references to the deleted `phase2_universe.csv`/`phase2_market.csv` files with `phase2_reference_universe.csv`/`phase2_reference_market.csv`, use the 2026-09-04 cutoff, and assert the two-member/40-field reference result. Keep the bad SEC CLI test synthetic and explicitly target a synthetic ticker/CIK.

- [x] **Step 2: Update the normal Phase 2 boundary test.**

  Read the checked-in reference CSVs instead of regenerating the ABC row, run the existing fixture refresh twice, and assert the AAPL/MSFT universe plus deterministic 40-field market result. Leave the configured-provider test synthetic because it tests injected adapter behavior rather than public facts.

- [x] **Step 3: Exercise the four SEC reference fixtures in the normalizer acceptance test.**

  Load each wrapped payload, pass its official source URL to the existing normalizer, assert the real ticker/CIK/accession/date/fact values, and ingest the resulting records against the reference universe. Keep malformed/mismatched SEC cases synthetic.

- [x] **Step 4: Verify synthetic adversarial coverage remains synthetic.**

  Confirm malformed/conflict/security tests retain their intentional `ABC`, `example.test`, control-vocabulary, and credential-shaped values where those values are the behavior under test; do not globally replace those controls.

### Task 3: Document fixture and retrieval boundaries

**Files:**
- Create: `docs/sources/fixtures.md`
- Modify: `docs/README.md`
- Modify: `docs/sources/phase2.md`

**Interfaces:**
- Documents the exact fixture paths, source URLs, symbols/CIKs, dates, retrieval date, official/unofficial status, redistribution caveats, transformations, and subset/survivorship limitations.
- Distinguishes the offline fixture/test flow, configured automatic API/RSS flow, and optional manual broker CSV compatibility flow.

- [x] **Step 1: Write the per-fixture provenance registry.**

  State that SEC payloads are official public API references, Yahoo market data is unofficial/reference data and was snapshotted locally because the endpoint was unavailable in this environment, the S&P page/methodology are official universe references, and RSS content is synthetic. Explicitly state that nothing is live production data and the AAPL/MSFT universe is not the complete or historically exhaustive S&P 500.

- [x] **Step 2: Update Phase 2 source status and commands.**

  Replace stale claims that checked-in Phase 2 fixtures are wholly synthetic, link `fixtures.md`, and show the reference fixture command with the 2026-09-04 cutoff. Explain that configured adapters are the automatic production retrieval path and manual broker CSV remains optional legacy compatibility only.

- [x] **Step 3: Update documentation examples without changing production behavior.**

  Keep all command examples under `docs/`, remove normal-path ABC/Synthetic Corp references, and retain synthetic labels only for explicit parser/adversarial examples.

### Task 4: Run the requested verification and hand off uncommitted changes

**Files:**
- Verify: all changed files and repository state.

- [x] **Step 1: Run the complete requested verification.**

  Run `python3 -m unittest discover -s tests -v`, `python3 -m compileall -q src tests`, `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m nisa_quant --help`, and `git diff --check`.

- [x] **Step 2: Run targeted provenance/security/offline checks.**

  Verify exact fixture metadata, scan changed fixtures/docs for credentials/secrets/private URLs and stale normal-path identifiers, inspect tests for network calls, run the normal reference fixture CLI flow, and confirm adversarial tests pass without production network access.

- [x] **Step 3: Verify scope and handoff state.**

  Confirm no files under `yume-ios` changed, `git status` shows only the intended uncommitted fixture/test/documentation diff, and report `status=awaiting_verification` without committing or pushing.
