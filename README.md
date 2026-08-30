# NISA Quant Assistant — first local slice

This standalone Python package is a local-first, advisory-only research tool. It imports only the documented synthetic broker CSV and local price fixtures, stores normalized records in SQLite, computes portfolio facts without a language model, screens with safe data-quality fallbacks, renders cited Markdown, and keeps append-only recommendation outcomes.

It has no network client, broker login, broker write access, order endpoint, dispatch path, scheduled delivery, mobile app, or Hermes integration. No profitability or live-data claim is made.

## Local commands

The repository uses a `src/` layout, so run the CLI with `PYTHONPATH=src`:

```bash
PYTHONPATH=src python3 -m nisa_quant --help
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

Fixture workflow:

```bash
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT
database="$temporary_directory/nisa-quant.sqlite"
report="$temporary_directory/nisa-report.md"
PYTHONPATH=src python3 -m nisa_quant init-db --db "$database"
PYTHONPATH=src python3 -m nisa_quant watchlist-add --db "$database" --identifier-value 1306 --identifier-type jpx_code --display-name "TOPIX ETF" --asset-type ETF --market JPX --currency JPY --benchmark TOPIX.BENCHMARK --benchmark-identifier-type other --benchmark-identifier-value TOPIX.BENCHMARK --notes synthetic --effective-date 2026-08-01 --observed-at 2026-08-01T00:00:00+00:00
PYTHONPATH=src python3 -m nisa_quant import-csv --db "$database" --csv tests/fixtures/synthetic_broker.csv
PYTHONPATH=src python3 -m nisa_quant import-prices --db "$database" --csv tests/fixtures/synthetic_prices.csv
PYTHONPATH=src python3 -m nisa_quant import-distributions --db "$database" --csv tests/fixtures/synthetic_distributions.csv
PYTHONPATH=src python3 -m nisa_quant snapshot --db "$database" --as-of 2026-08-30
PYTHONPATH=src python3 -m nisa_quant screens --db "$database" --as-of 2026-08-30
PYTHONPATH=src python3 -m nisa_quant report --db "$database" --as-of 2026-08-30 --output "$report"
```

Recommendation metadata can be recorded and evaluated later:

```bash
PYTHONPATH=src python3 -m nisa_quant record-recommendation --db "$database" --as-of 2026-08-30
PYTHONPATH=src python3 -m nisa_quant evaluate-recommendation --db "$database" --recommendation-id 1 --date 2026-09-30 --observed-source-id SRC-... --benchmark-source-id SRC-... --observed-price 1100 --benchmark-price 105
```

## Synthetic CSV contract

`tests/fixtures/synthetic_broker.csv` is intentionally synthetic and uses the exact Japanese headers:

`取引日,口座区分,銘柄コード,銘柄コード種別,銘柄名,取引区分,数量,単価,手数料,通貨,分配金,備考`

The importer rejects missing, extra, duplicate, reordered, and row-overflow fields. It does not guess identifier types or create `.T` aliases. Blank required numeric cells and unknown account labels are retained in `review_quarantine` with explicit warnings. Account labels are explicit: `NISA`, `特定口座`/`taxable`, `general`, `cash`, and `foreign_currency`; unknown labels are quarantined for review.

BUY/SELL prices must be finite and strictly positive; fees must be finite and non-negative. Invalid security rows are quarantined before source records, transactions, positions, or P/L can be created. Snapshot replay applies the same fail-closed validation to legacy/direct rows.

The price fixture stores explicitly typed dated prices, a benchmark, a stale observation, and conflicting source values. Its required provenance fields are identifier type, observation date, retrieval timestamp, currency, freshness status, and citation location; blank provenance is rejected. Source records retain source name/identifier, identifier type, retrieval time, observation date, field, value, unit/currency, freshness, parser version, and citation location. Retrieval cannot precede an observation. Repeating the same exact source record is idempotent; refreshed source metadata receives a distinct source identity. Source freshness is explicit: `current` and `observed` are usable statuses; `stale`, `conflicting`, and `unavailable` are retained for audit but cannot supply prices, benchmarks, distributions, yields, or directional metrics. Numeric source facts require a nonblank unit and currency, a real parser version, and a citation location; price and benchmark values must be strictly positive, while distribution amounts remain non-negative. Unknown or arbitrary freshness and incomplete metadata are rejected at ingestion (or remain quarantined by the broker importer).

The closed source fact fields are `price`, `benchmark_price`, `distribution`, `buy`, `sell`, and `cash_movement`; each security fact requires an explicit typed instrument identity. Cash movements are the only identity-free fact because they belong to an account rather than an instrument. Derived arithmetic is checked for finite results, so overflowed cost basis, market value, return, risk, distribution, and cash aggregates become unavailable with a warning or are quarantined.

`tests/fixtures/synthetic_distributions.csv` is the separate strict local path for watchlist/ETF distribution observations. Its exact columns are `identifier,identifier_type,observation_date,distribution_amount,unit,currency,retrieved_at,freshness_status,citation_location`, and the CLI entry point is `import-distributions`. The checked-in fixture uses `per_unit` observations for a synthetic non-personal ETF. Snapshots expose the latest distribution amount, change versus the prior accepted observation, and a yield only when a per-unit observation and usable price are both available. They retain distribution source IDs and a distribution data cutoff; missing or insufficient history stays unavailable with a warning. Broker `分配金` remains a transaction-level `total_cash` value and is never treated as per-unit data.

Snapshots use one conservative UTC cutoff policy: both the fact/observation date and the UTC calendar date of the source retrieval must be on or before `as_of`. ISO retrieval timestamps are normalized to timezone-aware UTC at ingestion; a legacy naïve timestamp is interpreted as UTC. This applies to transactions, dated warnings, prices, benchmarks, derived histories, recommendation snapshots, and later outcomes; undated warnings are current-context only and never enter a historical snapshot. Direct and legacy transaction replay is fail-closed unless the linked source has the expected typed field, usable freshness, compatible unit/currency, finite matching value, exact typed identity, and observation/retrieval chronology. Cash replay requires a matching finite `cash_movement` source rather than a price source. Contributions are the sum of positive explicit `CASH` movements; BUY expenditure is cost basis, not a contribution. Sells reduce average-cost basis by the average basis of the units sold, and realized P/L is tracked separately. `cost_basis_by_currency` and `realized_pl_by_currency` preserve exact currency-separated ledger facts; mixed-currency aggregate values remain unavailable without explicit cited FX. Missing, stale, conflicting, or insufficient history remains unavailable/warned.

The local watchlist stores explicit identifier type, asset type, market/exchange, currency, typed benchmark identifier, and notes in append-only effective/observed versions. The `watchlist-add` CLI command calls this existing versioned API; pass both `--benchmark-identifier-type` and `--benchmark-identifier-value` for a typed benchmark. A new item defaults to the current UTC effective date; historical imports must provide `effective_date` and, when needed, `observed_at`. Snapshots select only the latest version effective and available by the requested cutoff, so later edits cannot rewrite earlier reports. The materialized `watchlist` and `instruments` tables are rebuilt from versions available on the current UTC date; a future-effective append-only version remains hidden until its effective date. Legacy rows are migrated idempotently on every initialization; rows without historical metadata receive the initialization UTC date/instant rather than an ancient sentinel. Watchlist-only items appear in screens. Deterministic history context includes momentum, a three-observation moving average, drawdown, sample volatility (annualized from dated returns), price return, distributions separately, and the declared benchmark-relative return where enough local observations exist. Same-date duplicate source values choose the latest retrieval when numerically identical; disagreement within one explicitly linked typed-identity component makes the component unavailable while unrelated identities remain independent. Undeclared benchmarks are `UNDECLARED` and unavailable. These are research facts, not profitability claims or live signals.

The broker fixture's `分配金` field is a total cash amount for the transaction, not a per-unit amount. It is stored as source unit `total_cash` and is never multiplied by quantity. Portfolio totals and report citations retain that unit. Holdings in multiple currencies are kept in `*_by_currency` fields; aggregate market value, cost basis, allocation, concentration, and P/L are unavailable without an explicit, cited FX rate.

When the relevant snapshot contains more than one currency, scalar contributions, cash movements, distributions, and realized P/L are also unavailable without FX; their per-currency maps remain the reconciliable accounting record. The persisted `positions.cost_basis` is nullable for mixed-currency lots, so it never represents a fabricated cross-currency scalar. Import-time and snapshot replay both fail closed for impossible same-instrument sells: the accounting path is skipped with an explicit warning, while source records remain auditable.

Instrument and benchmark matching requires the exact identifier type and value. An alternate identifier is usable only through an explicit typed `link_instruments` row (the compatibility name `add_instrument_link` is also available); connected links are resolved as one component, and no `.T`, `.BENCHMARK`, or other alias is inferred. Same-date facts compare value, unit, and currency within that component; disagreement is unavailable/audit-only, while exact same-value controls and unrelated typed identities remain valid. This rule applies to prices, benchmarks, distributions, ledger replay, snapshot provenance/citations, candidate generation, recommendation binding, and later outcome evidence. A benchmark value without a benchmark type is `UNDECLARED` and unavailable. Portfolio risk uses a common dated observation series across holdings, exposes portfolio drawdown, and leaves portfolio volatility unavailable unless that dated series has enough returns. Required missing/conflicting prices or unsupported currency aggregation fail closed while retaining known-by-currency values. Reports validate every structured candidate field, metric, allowed label, citation, derivation binding, and exact provider/template contract; callers must supply the expected provider/model identifier and template version. Renderer-marked reports always require those metadata fields; the explicit unstructured/manual path has no renderer markers and still requires citations. When `snapshot` and `candidates` are supplied, the report must equal the canonical renderer output exactly; only format characters inside the three fixed marker labels are normalized, and whitespace, invisible characters, extra lines, prose, facts, filenames, and source IDs elsewhere are rejected. Both structured and unstructured paths reject unsupported action vocabulary, including normalized standalone or label-containing imperatives, PII-like filenames/account identifiers, and credential-shaped content while allowing ordinary research filenames and explanatory `token`. Outcome evaluation requires evidence with observation date after the recommendation cutoff, retrieval on or after both observation and cutoff dates, retrieval no later than the evaluation date, exact typed instrument/benchmark identity (or an explicit link), usable freshness, matching currency, and no same-date conflicts. The evaluation date is bounded by the two cited price/benchmark records only; unrelated source fields cannot authorize it. Optional numeric arguments are checked against cited values; they are never authoritative on their own. Recommendations store the generated snapshot, stable canonical snapshot hash, calculation/provider/template identifiers, typed instrument/benchmark/currency/freshness metadata, and source IDs for reproduction.

Outcome replay also validates the persisted snapshot against the strict finite JSON schema and recomputed canonical hash before writing an outcome; malformed, non-finite, or altered snapshots are rejected. Bare account, broker, customer, and portfolio identifiers are rejected in reports, while ordinary category/research prose remains allowed.

Legacy databases may still have nullable source identifier-type columns. Those rows are retained for audit but fail closed for typed security valuation and ledger calculations until explicitly backfilled; no migration guesses an identifier type. Pre-versioning watchlist rows are migrated idempotently on initialization, preserving valid effective/observed metadata; when no observed field exists, initialization seeds the current aware UTC instant and date because older history is unavailable. Valid, unambiguous legacy position cost basis is preserved on that migration.

## Safety and data quality

The only directional vocabulary permitted by this slice is `BUY CANDIDATE`, `HOLD`, `SELL / REDUCE CANDIDATE`, `WATCH`, and `NO ACTION / INSUFFICIENT DATA`. Material missing, stale, conflicting, or unavailable inputs prevent a directional screen result. Reports include cutoffs, warnings, source IDs, evidence quality, metrics, counter-evidence, invalidation conditions, and the statement: “Manual review required; no order was placed.”

Do not put real broker exports, account numbers, credentials, raw personal data, or generated personal reports in source control. The `.gitignore` protects local data/report paths; review any input filename before importing it.
