# Subagent 1 salvage — LLM frameworks, data sources, backtesting, Japan libs (deleg_c6446f93)

The subagent's final summary was lost to a search-guardrail stop, but its transcript
surfaced the key sources below. Facts cross-checked by supervisor (GitHub HTML/API + web).

## 1. LLM-driven stock analysis frameworks
- **TauricResearch/TradingAgents** — 99.5k★, pushed 2026-07-18 (v0.3.1: look-ahead filtering fixes, checkpoint resume, retry budget). Multi-agent "trading firm" (fundamentals/sentiment/news/technical analysts → bull/bear debate → trader → risk). Key verified facts:
  - Works with **any market Yahoo Finance covers using exchange-suffixed tickers** → Japanese `.T` tickers supported (e.g., 7203.T, 1321.T).
  - Data vendors: yfinance / Alpha Vantage / OpenAI / Google / local with automatic fallback (DeepWiki vendor-system docs).
  - LLM providers: OpenAI, Anthropic, Google, xAI, DeepSeek, Qwen, GLM, MiniMax, **OpenRouter**, Ollama, Azure.
  - CLI `analyze` with opt-in `--checkpoint` resume; results are non-deterministic between runs ("expected for a research tool").
- **virattt/ai-hedge-fund** — 63k★, pushed 2026-08-07. Multi-agent hedge-fund simulation (fundamentals/technical/sentiment/valuation agents, risk manager, portfolio manager). Requires Financial Datasets API key for prices/fundamentals/earnings (free only for AAPL/GOOGL/MSFT/NVDA/TSLA without key) + one LLM key (Anthropic/OpenAI/DeepSeek/Google/xAI/Kimi). US-centric; educational "proof of concept", not production trading.
- **virattt/dexter** — 27.5k★. Autonomous deep financial research agent; eval suite with LLM-as-judge. US-focused; see dexter-jp below.
- **edinetdb/dexter-jp** — ~300★, active (first release 2026). **Japan-specific fork of dexter**: EDINET DB (~3,800 companies' filings, 有報テキスト, screening) + optional J-Quants V2 (official TOPIX OHLC). Plans autonomously, cross-validates multiple sources, produces reports. Most directly relevant JP research agent found.
- **AI4Finance-Foundation/FinGPT** — 21.1k★, pushed 2026-08-02. Open financial LLMs; sentiment analysis focus.

## 2. Data sources for JPX tickers from Japan
- **yfinance (25k★, very active)** — Yahoo Finance API incl. Tokyo `.T` suffix tickers; free, unofficial, research-use terms.
- **J-Quants API V2 (JPX official)** — see subagent3 file for full plan details; Free tier = 12-week delay, ~2y history, 5 req/min.
- **kabu STATION API** — official broker API (see subagent3); Windows/same-IP constraints.
- **Rakuten MarketSpeed II RSS** — Excel add-in live data link (Windows/Excel only); not an API for agents.
- **SBI** — no public API located; CSV export path instead.
- pyjpx-etf / pykabutan / pyboj / kabupy — small JP-specific scraping/data libs (obichan117 suite; kabuyoho scraper reirev/kabupy).

## 3. Portfolio tracking/backtesting maintenance status (verified)
- **ranaroussi/quantstats** — 7.6k★, pushed 2026-07-20. Portfolio analytics (Sharpe, Sortino, drawdown, HTML reports) — best fit for monthly NISA portfolio reporting.
- **polakowo/vectorbt** — 8.8k★, pushed 2026-08-02. Fast vectorized backtesting at scale.
- **pmorissette/bt** — 3k★, pushed 2026-08-07. Flexible backtesting, good for rebalancing-strategy tests.
- **backtrader** — 22.9k★ but last push 2024-08 (maintenance stalled; community forks only).
- **quantopian/zipline** — archived-ish (last push 2024-02); use stefan-jansen/pyfolio-reloaded for analytics instead of dead pyfolio.
- **stefan-jansen/machine-learning-for-trading** — 20.6k★, actively maintained book repo (data→live execution).

## 4. Japan-retail-specific tools
- obichan117 suite: **pyjpx-etf** (JPX ETF PCF composition/ranking/screening), **pykabutan** (kabutan.jp scraper), **pykabu-calendar** (earnings calendar aggregator w/ official IR verification), **pyboj** (BOJ data). New/small but purpose-built and MIT-licensed.
- **10mohi6/jquants-pairs-trading-python** — J-Quants pairs-trading sample.
- zenn.dev/edinetdb EDINET+J-Quants guide (JP-language integration writeup).

## Supervisor note
The subagent hit the loop_web_search_cap guardrail before writing its summary;
contents above are salvaged from its verified source trail and re-verified by supervisor.
