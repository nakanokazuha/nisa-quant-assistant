# NISA Phase 2 Evidence Layer Design

## Status

Active design for task `nisa-phase2-r1`. This supersedes the historical
Japan-first live-provider deferral language for the Phase 2 slice while
preserving the existing local ledger, fixture, CSV, SQLite, and legacy
recommendation commands.

## Goal

Add a standalone, read-only evidence layer for US-listed S&P 500 constituent
equities. The layer accepts reproducible universe snapshots and injected or
configured provider observations, validates chronology and provenance, stores
accepted evidence idempotently in SQLite, and emits evidence/context output
without research synthesis, predictions, directional verdicts, or trade
actions.

## Architecture

The Phase 2 implementation is isolated in `nisa_quant.phase2` and
`nisa_quant.phase2_sources`. It uses the existing UTC and fail-closed source
validation principles but has separate tables because the legacy
`source_records` table cannot represent OHLCV, filing periods, article
metadata, or watch-alert semantics without weakening its closed fact contract.

`Phase2Refresh` coordinates four input boundaries: a strict S&P universe CSV,
a provider-neutral daily market provider, SEC submissions/company-facts
payloads, and explicitly configured RSS feeds. Providers return typed raw
observations or payloads; normalization is the only path to persistence.
`urllib` is optional runtime transport, restricted to an HTTPS host allowlist,
bounded timeout, no redirects, and no broker/private hosts. Tests inject a
transport or fixture provider and never need credentials.

## Stored contracts

- `phase2_universe_members`: ticker, CIK, issuer, exchange, effective date,
  membership status, source identifier/version, retrieval time, explicit
  look-ahead status, and survivorship-bias status.
- `phase2_market_observations`: one row per typed OHLCV field, observation date,
  UTC retrieval time, provider/request identity, currency, citation, and
  freshness status.
- `phase2_evidence`: filing/news/alert records with stable source identity,
  typed issuer/ticker mapping, publication and period fields, bounded evidence
  text or metadata, topic/category, source quality, recency, corroboration,
  uncertainty, and lag metadata.
- `phase2_failures`: explicit unavailable, malformed, ambiguous, unsupported,
  stale, or conflicting refresh outcomes; failures never become facts.
- `phase2_refresh_runs` and `phase2_snapshots`: replay identity, counts,
  warnings, canonical report JSON/hash, and the no-verdict boundary.

Idempotency is based on canonical provider observation/request identity. Exact
replays use `INSERT OR IGNORE`; changed provider metadata or values remain
distinct evidence and conflicts are surfaced rather than silently selected.

## Provider capability decisions

- Fully implemented: strict local universe and market/SEC/RSS fixture adapters,
  normalization, persistence, report generation, and failure auditing.
- Implemented/configuration-required: Alpha Vantage daily OHLCV adapter. Its
  documented `TIME_SERIES_DAILY` response supports daily OHLCV, but the API key
  is read only from caller configuration and is never persisted or logged.
- Implemented/configuration-required: SEC submissions and Company Facts JSON
  adapter using explicit CIK/ticker mappings. Only documented form metadata and
  supplied XBRL facts are accepted; no fundamental value is inferred.
- Implemented/configuration-required: RSS metadata/bounded-excerpt adapter.
  A feed must be explicitly configured with terms/content policy and entity
  aliases; ambiguous or unmapped articles become failures.
- Deferred: paywalled/licensed news, broker data, options/short-interest
  vendors, issuer-specific scraping, live S&P constituent downloads, and any
  source whose endpoint, licensing, or field semantics are not documented.

## Boundary

Phase 2 report output contains only universe membership, market observations,
filing/news/watch-alert evidence, freshness, provenance, and warnings. A
guard rejects directional action labels, execution vocabulary, order-shaped
payloads, broker identifiers, and credential-shaped content before report
serialization. Existing legacy recommendation functionality remains available
for compatibility but is not called by Phase 2 refresh/evidence commands.

## Testing

Tests use real SQLite connections and injected fixture/provider payloads. They
cover duplicate replay, malformed/partial values, chronology and UTC handling,
conflicts, stale/unavailable sources, ambiguous ticker mapping, article and
filing identity deduplication, insider/ownership publication lag, unsupported
flow proxies, snapshot hash integrity, and the explicit no-verdict/no-trade
boundary. A temporary-directory CLI flow proves deterministic refresh through
evidence output with no secrets.
