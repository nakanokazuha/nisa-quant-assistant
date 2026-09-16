# Phase 3 operations

The project requires Python 3.11+ and has no extra model dependency. This is deliberate: the specialized model is a deterministic standardized diagonal ridge-like fit implemented in the package, not a true multivariate ridge solve and not LightGBM/scikit-learn, because the current minimal environment does not provide those libraries and the workflow must remain easy to install and replay.

Status vocabulary: implemented items are the local deterministic model, cache/replay, explicit live market adapter, point-in-time labels, and bound reports; live-provider-risk covers Yahoo availability, licensing/terms, current-universe retrieval, and SEC response coverage; partial/unavailable results identify failed or insufficient tickers and do not fall back to the six offline fixtures; deferred items include historical membership, full SEC coverage, calibrated uncertainty, fully cited live fundamentals, scheduling, and trading/broker writes.

The release status/exit contract is: `available` and `available_descriptive` are successful reports and exit 0; `unavailable_insufficient_data` is a completed but insufficient-data report and exits 2; `unavailable` is a provider, cache, range, or artifact failure and exits 2. Current-only descriptive output may retain `current_predictions` while suppressing performance claims. `phase3-refresh` is an exposed command, replay-only by default, and replay filters content-addressed history caches by the complete canonical request contract and exact requested range. Unrelated snapshots are ignored; zero or multiple compatible matches produce structured unavailable output. A caller with the full contract may pass `--request-contract`; replay never relabels a candidate to fit the request. A live run without `--limit` is `full_current_sp500`; `--limit N` is only `limited_live_smoke` and must never be described as the full universe. The full current S&P 500 fetch applies a documented conservative minimum of 450 unique members; the low-level parser's smaller threshold is test-only compatibility.

Provider failures are retained in the manifest by ticker. Partial live responses can be evaluated in memory against coverage thresholds, but no partial history snapshot is saved or accepted for replay.

SEC facts are bound by a versioned `sec_request_contract` carried inside each history snapshot. The contract records the requested ticker/CIK when available, provider and source version, requested range, `as_of`, retrieval intent/status, retrieval timestamp, and an exact hash of the serialized facts payload. SEC cache reuse requires a matching contract and the 31-day retrieval-freshness policy; live refreshes bypass market-cache reuse and rebind SEC facts to the current probe result. `not_requested`, `bound_no_usable_facts`, and provider failure states remain explicit after loading.

Every accepted SEC fact has non-empty accession/evidence identity, retrieval timestamp, source, citation, verified issuer identity, and filed-period chronology. Rehashed content-addressed caches do not authorize facts whose provenance contract is absent or malformed.

Malformed provider/data `ValueError`, `OverflowError`, and `ZeroDivisionError` exceptions are converted to the structured `unavailable` report and manifest failure evidence; raw exceptions are not exposed to the CLI caller.

Manifest `gaps` is a deterministic per-ticker object derived from snapshot coverage. Each entry records `status` (`covered`, `gapped`, `missing`, or `failed`), the actual observation bounds, row count, gap count, and the snapshot's observed gap ranges. Provider error text remains in `failures_by_ticker`, and panel eligibility remains in `panel_excluded_by_ticker`; these fields are not collapsed into `gaps`.

The producer preserves `unavailable_insufficient_data` when a validated backtest reports it; the bound report and producer exit are both status 2. A current-only descriptive report may retain actual current predictions while suppressing historical claims; when its serialized periods are empty it remains `unavailable_insufficient_data` and exits 2. Current predictions require actual finite market observations at the latest decision date. Zero-history or provider-failed members remain in manifest coverage/failure and panel-exclusion fields and never enter the panel or ranking.

## Install and offline verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests
```

The Phase 3 implementation has no third-party runtime dependency, so the `PYTHONPATH=src` commands are the reproducible current-environment installation path. If a network-enabled environment wants an editable package environment, `uv sync` also follows the existing `pyproject.toml`; package build dependencies are fetched by uv in that environment. The repository's existing Phase 2 and legacy fixture commands remain supported. Normal tests are offline. Generated files belong only under ignored `data/` and `reports/` paths.

## Current CLI surface

```python
PYTHONPATH=src python3 -m nisa_quant phase3-backtest \
  --dataset data/phase3/dataset.json \
  --output reports/phase3/backtest.json
```

The CLI reads an existing local training dataset and writes a deterministic backtest artifact. It does not fetch data, log in to a broker, or create an order path.

Responsibility-level producer, replay-only by default:

```bash
PYTHONPATH=src python3 -m nisa_quant phase3-refresh \
  --as-of 2026-09-16 --start 2024-01-01 --end 2026-09-16 \
  --cache-dir data/phase3 --output reports/phase3/report.json \
  --replay-only
```

Replay requires exactly one compatible local history cache and performs no network access. It validates each candidate's canonical request contract, filename address, and requested range before loading it. Explicit live refresh is opt-in:

```bash
PYTHONPATH=src python3 -m nisa_quant phase3-refresh \
  --as-of 2026-09-16 --start 2024-01-01 --end 2026-09-16 \
  --cache-dir data/phase3 --output reports/phase3/report.json \
  --live --sec-contact researcher@example.com
```

Use `--limit N` only for an explicit small public-source smoke; omit it for the full current S&P 500 path. Live output records actual constituent count, current-universe URL/retrieval timestamp, provider/request contract, benchmark, observation bounds, rows/gaps/failures, and artifact IDs in an atomic `*.manifest.json`. Current-only membership is prominently survivorship-limited and suppresses alpha/excess/benchmark-relative performance claims. The manifest reports `universe_scope=full_current_sp500` only when no limit was used.

The refresh report also includes `current_predictions`, ranked from the latest decision-date rows at or before `--as-of`. Each `model_score` is deterministic model output for descriptive research ranking only: it is not a recommendation, not a calibrated probability, and not evidence of future outperformance. When the rows come from the current survivor universe, the ranking is survivorship-limited; historical performance claims remain structurally suppressed while the current ranking remains visible.

Current predictions use the deterministic monthly `monthly_decision_date_31_calendar_day_allowance_v1` policy: the latest usable observation may be no more than 31 calendar days before `--as-of`. This preserves point-in-time feature chronology while allowing a monthly decision on the first of the month to remain current during the same calendar-month publication window. Stale, malformed, or future-dated series are excluded; the manifest and every emitted prediction include `current_prediction_freshness` evidence with the as-of date, observation date, measured age, status, policy, and threshold.

Replay validates every serialized asset and benchmark bar against the request contract's inclusive `start_date`/`end_date` bounds after loading, including content-addressed caches whose snapshot identity has been recomputed. An out-of-range bar is an unavailable replay/artifact failure with exit 2; bars are never clipped or relabeled. Strict report rendering likewise regenerates period rows from the validated serialized dataset and bound model artifact, and requires every serialized period and metric field to match that canonical result exactly.

Example current-output command:

```bash
PYTHONPATH=src python3 -m nisa_quant phase3-refresh \
  --as-of 2026-09-16 --start 2024-01-01 --end 2026-09-16 \
  --cache-dir data/phase3 --output reports/phase3/report.json \
  --replay-only
```

The model target is `target_3m_excess_return`, preserving the three-month prediction horizon; no rank-IC metric is calculated. Portfolio and benchmark observations use the non-overlapping `target_1m_return` and `target_1m_benchmark_return` price-return fields. Each interval is `[start,end)` and ends at the next monthly decision date; endpoint availability is retained in the dataset rows. Training purges any 3-month label whose forward endpoint is after validation start. Transaction costs are applied to monthly turnover using the symmetric-difference/current-selection convention. Current-only survivor-selected runs are descriptive evidence and suppress benchmark-relative/performance claims. Backtest JSON includes the calculated per-period and cumulative price returns, benchmark-relative returns where permitted, turnover, cost basis, return basis, interval convention, and artifact bindings; it does not claim uncalculated rank IC, annualized return, drawdown, volatility, hit rate, or validation-error metrics.

## Python API workflow and replay

```python
from pathlib import Path
from nisa_quant.historical_market_data import fetch_history_snapshot, load_history_snapshot
from nisa_quant.training_dataset import build_monthly_panel, save_training_dataset
from nisa_quant.ranking_model import SpecializedRankingModel, save_model
from nisa_quant.walk_forward_evaluation import save_backtest, walk_forward_backtest
from nisa_quant.phase3_reporting import render_phase3_report

history = fetch_history_snapshot(tickers=["AAA"], cache_dir=Path("data/phase3"))
history = load_history_snapshot(Path("data/phase3/history-<key>.json"), expected_request_contract=history.request_contract)
dataset = build_monthly_panel(history, as_of="2026-09-15")
save_training_dataset(dataset, Path("data/phase3/dataset.json"))
model = SpecializedRankingModel()
artifact = model.fit(dataset.rows, training_cutoff="2025-09-15", dataset_id=dataset.dataset_id, history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=history.benchmark_ticker)
save_model(artifact, Path("data/phase3/model.json"))
backtest = walk_forward_backtest(dataset)
save_backtest(backtest, Path("reports/phase3/backtest.json"))
Path("reports/phase3/report.md").write_text(render_phase3_report(dataset=dataset, model=model.artifact, backtest=backtest), encoding="utf-8")
```

The Python `fetch_history_snapshot` call fetches through the bounded read-only adapter or replays a valid matching cache when present; `load_history_snapshot` independently verifies the versioned request contract, roles, content identity, and cache filename. The `phase3-refresh --replay-only` command is the fresh offline smoke path; `--live` is the only network-enabled mode. A provider failure or insufficient history produces a diagnostic/unavailable report and no fabricated artifact. The model and report are advisory research artifacts only. There is no broker login, write API, order endpoint, automatic trading, or auto-buy/sell path.
