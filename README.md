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
PYTHONPATH=src python3 -m nisa_quant init-db --db /tmp/nisa-quant.sqlite
PYTHONPATH=src python3 -m nisa_quant import-csv --db /tmp/nisa-quant.sqlite --csv tests/fixtures/synthetic_broker.csv
PYTHONPATH=src python3 -m nisa_quant import-prices --db /tmp/nisa-quant.sqlite --csv tests/fixtures/synthetic_prices.csv
PYTHONPATH=src python3 -m nisa_quant snapshot --db /tmp/nisa-quant.sqlite --as-of 2026-08-28
PYTHONPATH=src python3 -m nisa_quant screens --db /tmp/nisa-quant.sqlite --as-of 2026-08-28
PYTHONPATH=src python3 -m nisa_quant report --db /tmp/nisa-quant.sqlite --as-of 2026-08-28 --output /tmp/nisa-report.md
```

Recommendation metadata can be recorded and evaluated later:

```bash
PYTHONPATH=src python3 -m nisa_quant record-recommendation --db /tmp/nisa-quant.sqlite --as-of 2026-08-28
PYTHONPATH=src python3 -m nisa_quant evaluate-recommendation --db /tmp/nisa-quant.sqlite --recommendation-id 1 --date 2026-09-30 --observed-price 1100 --benchmark-price 105
```

## Synthetic CSV contract

`tests/fixtures/synthetic_broker.csv` is intentionally synthetic and uses the exact Japanese headers:

`取引日,口座区分,銘柄コード,銘柄名,取引区分,数量,単価,手数料,通貨,分配金,備考`

The importer rejects missing, extra, duplicate, or reordered headers. It does not guess mappings. Account labels are explicit: `NISA`, `特定口座`/`taxable`, `general`, `cash`, and `foreign_currency`; unknown labels are quarantined as `unknown_review`. JPX numeric codes and Yahoo-style `.T` symbols are stored as distinct identifier records and linked only by an explicit local alias row.

The price fixture stores dated prices, a benchmark, a stale observation, and conflicting source values. Source records retain source name/identifier, retrieval time, observation date, field, value, unit/currency, freshness, and citation location. Re-importing the same file hash is idempotent.

## Safety and data quality

The only directional vocabulary permitted by this slice is `BUY CANDIDATE`, `HOLD`, `SELL / REDUCE CANDIDATE`, `WATCH`, and `NO ACTION / INSUFFICIENT DATA`. Material missing, stale, conflicting, or unavailable inputs prevent a directional screen result. Reports include cutoffs, warnings, source IDs, evidence quality, metrics, counter-evidence, invalidation conditions, and the statement: “Manual review required; no order was placed.”

Do not put real broker exports, account numbers, credentials, raw personal data, or generated personal reports in source control. The `.gitignore` protects local data/report paths; review any input filename before importing it.
