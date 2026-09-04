# Phase 2 fixture provenance

These files are small, deterministic, checked-in test references. They make
the normal Phase 2 fixture flow reproducible without network access. They are
not live production data, investment advice, a complete S&P 500 membership
file, historically exhaustive membership evidence, or a licensed market-data
distribution. Production retrieval remains automatic through the configured
API/RSS adapters. The legacy manual broker CSV importer is a separate optional
path and is not represented by these fixtures.

All fixture retrieval metadata uses the deterministic UTC value
`2026-09-04T00:00:00+00:00`, the task's documented retrieval snapshot date.
No API key, User-Agent secret, credential, private URL, broker data, or raw
personal data is included.

## Universe reference

File: `tests/fixtures/phase2_reference_universe.csv`

- Reference subset: AAPL / CIK `0000320193` and MSFT / CIK `0000789019`.
- Effective date and retrieval date: `2026-09-04`.
- Source: S&P Dow Jones Indices index page
  <https://www.spglobal.com/spdji/en/indices/equity/sp-500/> and methodology
  <https://www.spglobal.com/spdji/en/methodology/article/sp-us-indices-methodology/>.
- Status: official index/methodology reference URLs, but the checked-in rows
  are explicitly `current_snapshot_only` with
  `survivorship_risk_disclosed`.
- Transformation: the two selected symbols and issuer metadata are represented
  in the repository's strict universe CSV contract. No claim is made that the
  two rows are the complete index or prove historical membership on the
  effective date.

## Market reference

File: `tests/fixtures/phase2_reference_market.csv`

- Symbols: AAPL and MSFT; currency `USD`.
- Observation range: `2024-01-02` through `2024-01-05`, daily OHLCV, four rows
  per symbol.
- Intended reference endpoint: Yahoo Finance chart API, one URL per symbol:
  <https://query1.finance.yahoo.com/v8/finance/chart/AAPL?period1=1704153600&period2=1704585600&interval=1d&events=history>
  and
  <https://query1.finance.yahoo.com/v8/finance/chart/MSFT?period1=1704153600&period2=1704585600&interval=1d&events=history>.
- Verification: the Yahoo Finance chart endpoint was directly verified as
  accessible by the supervisor on `2026-09-04`. The checked-in OHLCV rows are
  rounded from that endpoint's raw numeric values: OHLC to two decimal places
  and volume retained exactly.
- Status and caveat: unofficial/reference market data only; the Yahoo endpoint
  is not claimed as official or licensed market data for production. The values
  were transformed into the strict Phase 2 `ticker, observation_date, open,
  high, low, close, volume, currency, retrieved_at, citation` columns. No
  adjusted-close field or derived signal was added.
- Freshness: because the historical rows predate the fixture cutoff by more
  than the Phase 2 freshness window, a refresh audits them as stale and does
  not emit them as usable current market observations. That outcome is
  intentional and deterministic.

## SEC reference extracts

The four JSON files below are small selected payloads, not full SEC API dumps.
The source is official SEC EDGAR JSON/API and filing material:
<https://data.sec.gov/submissions/CIK0000320193.json>,
<https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json>,
<https://data.sec.gov/submissions/CIK0000789019.json>, and
<https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json>.

### AAPL

- `phase2_reference_sec_aapl_submissions.json`: CIK `0000320193`, selected
  Form 10-K accession `0000320193-23-000106`, filed `2023-11-03`, report date
  `2023-09-30`, primary document `aapl-20230930.htm`; official filing citation
  <https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm>.
- `phase2_reference_sec_aapl_companyfacts.json`: same CIK and accession,
  selected
  `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax` period
  `2022-09-25` through `2023-09-30`, filed `2023-11-03`, value
  `383285000000` USD.

### MSFT

- `phase2_reference_sec_msft_submissions.json`: CIK `0000789019`, selected
  Form 4 accession `0000789019-26-000141`, filed `2026-08-05`, report date
  `2026-08-04`, primary document `form4.html`; official filing citation
  <https://www.sec.gov/Archives/edgar/data/789019/000078901926000141/form4.html>.
- `phase2_reference_sec_msft_companyfacts.json`: same target CIK, selected
  `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax` period
  `2022-07-01` through `2023-06-30`, filed `2023-07-27`, value
  `211915000000` USD. The wrapper preserves the official source accession
  `0000950170-23-035122`; the selected fact omits raw `accn` because the
  existing normalizer intentionally rejects a non-target-CIK accession prefix.

Transformation for both issuers: only the selected `recent` submission record
and one Company Facts unit/period record needed by normalization tests were
retained. Source URL, target ticker/CIK, accession/filing/report dates, and
fixture retrieval metadata are preserved in each wrapper. No User-Agent value
or secret is stored.

## Synthetic RSS parser control

File: `tests/fixtures/phase2_synthetic_news.xml`.

This is deliberately local synthetic XML using `example.test`, dated
`2026-09-01`, with an explicit description that it is synthetic parser
content. It exists to exercise RSS normalization and is not a retrieved public
historical feed item. Malformed, conflict, credential, redirect, and other
security-boundary fixtures likewise remain synthetic because they test behavior,
not public facts.

## Separation of paths

- Fixture/test path: `phase2-refresh-fixtures` with the files above; offline and
  deterministic.
- Configured automatic path: `phase2-refresh-config` with caller-supplied API
  keys/User-Agent/terms and configured Alpha Vantage, SEC, and RSS adapters;
  live retrieval is environment- and configuration-dependent.
- Optional manual broker path: the legacy `import-csv` command and its local
  synthetic broker fixture; it is not automatic provider retrieval and must not
  contain real account exports in source control.
