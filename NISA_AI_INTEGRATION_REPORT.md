# Historical research / deferred — not active implementation instructions

Release boundary: Implemented now: local fixture/CSV/SQLite/CLI only. Deferred roadmap: Hermes/live providers/scheduling/delivery. Prohibited: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

# NISA × AI Integration Research — Final Report

**Date:** 2026-08-24 · **Prepared by:** Historical research artifact from Yume/Hermes (the `stealth/ox-alpha` string is historical metadata only, not active configuration or a release rule).

## Release boundary (authoritative for the current tree)

Implemented now: local fixture/CSV/SQLite/CLI only.

Deferred/not implemented: Hermes runtime integration, live providers, scheduling, and delivery.

Prohibited boundaries: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

This report preserves research conclusions and future options. Its proposals, model names, provider references, and architecture diagrams are historical/deferred research only; they are not active routing instructions or dependencies of the local release.

**Goal:** Identify open-source, high-star, proven tools/plugins/repositories for stock & ETF research, signals, and portfolio analytics that Ilham can integrate into Hermes to support **NISA investing** (long-term, monthly accumulation; Japanese stocks + ETFs like 1321/1570/2559/2666, individual names).

---

## 1. Executive Summary

The ecosystem splits into four layers:

1. **Data** (what feeds the agent): J-Quants API V2 (JPX official) is the best Japan-native source; yfinance covers `.T` tickers for free.
2. **Frameworks** (heavy analysis engines): TradingAgents (99.5k★), ai-hedge-fund (63k★), Qlib+RD-Agent (47.9k★) — impressive but research-grade, LLM-costly, and built around US markets/trading frequency that does not match NISA buy-and-hold.
3. **Analytics** (deterministic math): QuantStats, vectorbt, bt — small, maintained, perfect for portfolio reporting and strategy sanity-checks.
4. **Agent glue**: jquants-mcp and edinet-db-mcp plug official Japanese data directly into Hermes via MCP — the highest-value, lowest-risk integration found.

**Historical verdict:** Don't wire an "AI hedge fund" to your NISA account. The research recommended data plus deterministic analytics with a human gate; that proposed Hermes narrative workflow is deferred. Evidence (§6): LLM timing strategies lose to buy-and-hold in long-run studies; NISA's tax structure makes loss-harvesting automation pointless anyway.

---

## 2. Verified Repository Comparison

Stars = GitHub snapshot 2026-08-23/24 (API + HTML cross-check). "Fit" = 1–10 for a Japan-based NISA long-term investor running Hermes.

### Tier A — directly useful to integrate

| Repo | Stars | Maintained | What it is | Data needs | Fit |
|---|---|---|---|---|---|
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) | 99,531 | ✅ 2026-07 | Multi-agent LLM "trading firm" (analysts→debate→trader→risk). Works with **any Yahoo-Finance market via suffixed tickers → supports `.T`**. Providers incl. OpenRouter/Ollama. CLI + checkpoint resume. | yfinance/AlphaVantage fallback chain + 1 LLM key | **7** as occasional deep-analysis engine (costly per run, non-deterministic) |
| [OpenBB-finance/OpenBB](https://github.com/OpenBB-finance/OpenBB) | 72,221 | ✅ 2026-07 | Open data platform for analysts/quants/**AI agents**; MCP server exposure ("connect once, consume everywhere") | Optional provider keys; free tier usable | **7** — heavier than needed for one portfolio, excellent data plumbing |
| [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | 63,011 | ✅ 2026-08 | Multi-agent hedge-fund simulation; educational PoC, explicit no-real-trades DNA | Financial Datasets API key (US-centric); LLM key | **5** US-first; JP tickers not first-class |
| [microsoft/qlib](https://github.com/microsoft/qlib) + RD-Agent (14.3k★) | 47,879 | ✅ 2026-07 | AI quant platform (ML/RL factor models); RD-Agent automates factor/model R&D | Historical market data pipelines; heavy infra | **4** overkill for NISA scale; world-class learning resource |
| [ranaroussi/yfinance](https://github.com/ranaroussi/yfinance) | 25,062 | ✅ 2026-08 | Yahoo Finance downloader — **JPX `.T` tickers work**; unofficial, research terms | none | **9** — zero-cost price backbone for JP ETFs/stocks |
| [AI4Finance-Foundation/FinGPT](https://github.com/AI4Finance-Foundation/FinGPT) | 21,139 | ✅ 2026-08 | Open financial LLMs; sentiment models for headlines/filings | GPU or inference endpoint | **6** sentiment layer for JP news needs JP-model adaptation (uncertain) |
| [stefan-jansen/ml-for-trading](https://github.com/stefan-jansen/ml-for-trading) | 20,594 | ✅ active | ML-for-Trading book code: data→features→models→backtests | mixed | **6** reference/learning |
| [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) | 14,321 | ✅ 2026-08 | Automated quant R&D agent on top of Qlib | qlib stack, Linux-preferred | **3** fascinating, wrong altitude for personal NISA |
| [polakowo/vectorbt](https://github.com/polakowo/vectorbt) | 8,795 | ✅ 2026-08 | Vectorized backtesting at scale (Numba/Rust) | pandas/numpy | **7** test momentum/crash-guard ideas fast |
| [ranaroussi/quantstats](https://github.com/ranaroussi/quantstats) | 7,578 | ✅ 2026-07 | Portfolio analytics: Sharpe/Sortino/drawdown/HTML tear sheets | returns series | **9** monthly NISA performance report engine |

### Tier B — Japan-specific (small but purpose-built)

| Repo | Stars | What it is | Fit |
|---|---|---|---|
| [J-Quants/jquants-api-client-python](https://github.com/J-Quants/jquants-api-client-python) | 198 | Official JPX API SDK (ClientV2, pandas). Free tier: daily prices/financials summary/earnings dates with **12-week delay**, ~2y history, 5 req/min. Paid ¥1,650+/mo removes delay. | **9** (weekly research pipeline) |
| [edinetdb/dexter-jp](https://github.com/edinetdb/dexter-jp) | ~300 | **Japanese-stock autonomous research agent** — EDINET DB (~3,800 companies) + optional J-Quants V2; plans, cross-verifies, reports. Fork of virattt/dexter (27.5k★). | **8** ready-made JP deep-research pattern |
| [obichan117/pyjpx-etf](https://github.com/obichan117/pyjpx-etf) | <50 (new) | JPX ETF PCF composition, holdings lookup (`etf find 7203`), ranking/screening. MIT. | **7** ETF due-diligence helper |
| obichan117/pykabutan / pykabu-calendar / pyboj / reirev/kabupy | <50 | Kabutan scraper, earnings-calendar aggregator w/ IR verification, BOJ data, kabuyoho scraper. | **6** supplementary scrapers (fragility risk) |
| [shigechika/jquants-mcp](https://github.com/shigechika/jquants-mcp) | ~2 (new) | **MCP server: 54 tools over J-Quants V2** (prices, screeners, valuation, earnings, charts). Free plan enough for charts/screener/overview. | **9** drop-in for Hermes |
| [edinetdb/edinet-db-mcp](https://github.com/edinetdb/edinet-db-mcp) | new | Remote MCP for EDINET filings; free API tier (EDINETDB_API_KEY). Companion ai-berkshire-jp skill. | **8** cited fundamental research |
| [kabucom/kabusapi](https://github.com/kabucom/kabusapi) | 433 | Official kabu STATION API portal (positions/orders; needs Windows gateway + Pro/Premium plan, same-IP). | **3–6** only if MUFG eSmart customer |

### Reference lists (discovery feeds)
[wilsonfreitas/awesome-quant](https://github.com/wilsonfreitas/awesome-quant) 29.1k★ · [georgezouq/awesome-ai-in-finance](https://github.com/georgezouq/awesome-ai-in-finance) 6.4k★ · [LLMQuant/awesome-trading-agents](https://github.com/LLMQuant/awesome-trading-agents) 388★ (skills explicitly for Claude/Hermes/Codex) · [Tom-roujiang/Awesome-LLM-Quant-Trading-Papers](https://github.com/Tom-roujiang/Awesome-LLM-Quantitative-Trading-Papers) 231★

### Out of scope (noted, excluded)
freqtrade 53.6k★ / jesse 8.4k★ (crypto bots), zipline/backtrader (maintenance stalled: last pushes 2024), gs-quant (needs Goldman credentials), GMO Coin API (crypto).

---

## 3. Broker Reality Check (Japan)

| Broker | Public retail API? | Practical path for Hermes | Fit |
|---|---|---|---|
| Rakuten | ❌ none located (uncertain≠impossible) | CSV export → local ledger | 3 direct / 8 CSV |
| SBI | ❌ none located | CSV export → local ledger | 3 direct / 8 CSV |
| Monex | ⚠️ OAuth API (2019) + **MONEX MCP Server announced Aug 2026** (ChatGPT/Claude account access) | If you bank with Monex: read-oriented account checks | **9 if Monex user**, else 0–4 |
| kabu.com (MUFG eSmart) | ✅ full REST/WebSocket | Windows sidecar + same-IP + Pro/Premium plan required | 6 (3 Mac-only) |
| Money Forward ME | Aggregator only, no open API assumption | Portfolio snapshot source | 7 snapshot / 4 API |

**Takeaway:** For Rakuten/SBI (the common NISA brokers), the honest architecture is **CSV export → normalized SQLite/Portfolio Performance ledger → Hermes analysis**. Auditable, broker-proof, no scraping fragility.

---

## 4. Deferred future integration proposals (research only; not implemented)

### #1 — Weekly J-Quants market brief (fit 9/10) *(start here)*
J-Quants Free account → `jquants-api-client-python` → local SQLite cache → deterministic metrics (index levels, volatility, drawdown from peaks, USDJPY) → **Codex Luna-backed Hermes workflow writes a cited Markdown brief** delivered to Discord every Sunday evening via cron. 12-week delay is irrelevant for weekly long-term context. Cost: ¥0.

### #2 — Portfolio ledger + reconciliation (fit 9/10)
Broker CSV exports → normalizer script (security codes, NISA/特定口座 labels, buys, distributions) → SQLite → QuantStats monthly report (Sharpe, max drawdown vs 1321 benchmark) → Hermes explains changes in plain Indonesian/Japanese. Deterministic numbers, LLM narration only.

### #3 — MCP wiring: jquants-mcp + edinet-db-mcp (fit 8–9/10)
Two MCP servers give Hermes live tool-calls into official JPX prices and EDINET filings. Ask "apa yang berubah di laporan Toyota kuartal ini?" and get a cited answer instead of hallucinated numbers. Free tiers sufficient.

### #4 — Distribution/earnings monitor (fit 8/10)
Watch issuer pages for held ETFs (1321 NEXT FUNDS, 2559 MAXIS All-Country) + J-Quants earnings dates → alerts for ex-date, payment date, missing/unexpected distributions. Pure reminder service — zero trading temptation.

### #5 — Occasional deep-dive via TradingAgents (fit 7/10, conditional)
For a single-ticker thesis check (e.g., before adding 7203 or ORIX beyond index), run TradingAgents locally with `.T` ticker + OpenRouter provider. Accept: multi-agent run costs tokens, output varies between runs, treat as *structured second opinion*, never as signal.

### Explicitly NOT recommended
- ❌ Automated buying/selling or order placement from Hermes (no broker write-API should be wired; FinRA cautions; StockBench shows agents rarely beat hold).
- ❌ Tax-loss harvesting automation — **meaningless inside NISA** (losses can't offset other gains or carry forward).
- ❌ Daily LLM "signals" — evidence says they underperform buy-and-hold long-run (arXiv 2505.07078) and backtest alpha often fails deployment checks ("Alpha Illusion", arXiv 2605.16895).
- ❌ Crypto bot stacks (freqtrade/jesse/GMO API) — outside mandate.

---

## 5. NISA-Specific Guardrails for Any Build

1. Ledger separates NISA / 特定口座 / general accounts — contribution-limit tracking stays deterministic.
2. Action vocabulary: `monitor / review / rebalance candidate / distribution reminder / missing data` — never `buy now / sell now`.
3. Every number in any report must trace to J-Quants, issuer page, broker CSV, or computed code — LLM never invents figures (FinanceBench: retrieval LLMs misanswer most filing questions).
4. Weekly/monthly cadence only; benchmark everything against 1321 or TOPIX total return.
5. Keep broker credentials out of prompts/logs; read-only paths only; human confirms anything executable.

---

## 6. Honest-Limitations Evidence (why this design is conservative)

| Source | Finding |
|---|---|
| arXiv 2505.07078 (long-run LLM strategies) | Buy-and-hold significantly outperformed both tested LLM strategies after bias mitigation |
| StockBench (arXiv 2510.02209) | Most LLM agents fail to beat buy-and-hold in multi-month sequential trading |
| The Alpha Illusion (arXiv 2605.16895) | Reported LLM-agent alpha typically collapses under leakage/cost/benchmark checks |
| FinanceBench (arXiv 2311.11944) | Frontier LLM w/ retrieval answered/refused incorrectly on majority of filing QA in one config |
| Counter-evidence | Some studies show ChatGPT screening correlates with returns (arXiv 2308.06260) — useful for *narrowing research*, not automating allocation |

---

## 7. Historical research conclusion (not the active release architecture)

**Build the boring stack.** The winning combination for your NISA workflow is not the 99k-star trading circus — it is:

```text
yfinance (.T tickers, free)      ─┐
J-Quants V2 free tier (official)  ├─► SQLite cache ─► deterministic metrics
broker CSV exports               ─┘        │
                                           ▼
                    QuantStats report + jquants-mcp / edinet-db-mcp lookups
                                           │
                                           ▼
              Codex Luna weekly cited brief → Discord (cron, human decides)
```

- **Highest value/effort ratio:** J-Quants weekly brief (#1), then CSV ledger (#2).
- **Best "wow" layer:** MCP servers (#3) — real cited fundamentals on demand.
- **Skip for now:** TradingAgents runs, qlib/RD-Agent, any order-execution idea.
- **Total software cost:** ¥0 (all free tiers); only optional spend later is J-Quants Light ¥1,650/mo if 12-week delay ever bothers you.

Everything above is grounded in verified repo state and official docs as of today; star counts drift, so re-check before adopting. Research artifacts live in `/Users/user/Documents/Yume/nisa-research/`.

*Not financial advice — this is tooling research. The market will still do what it wants; Hermes just makes sure you see it clearly.*
