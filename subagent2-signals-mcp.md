# Historical research / deferred — not active implementation instructions

Release boundary: Implemented now: local fixture/CSV/SQLite/CLI only. Deferred roadmap: Hermes/live providers/scheduling/delivery. Prohibited: credentials, broker login/write/order execution, Discord delivery, and Yume iOS changes.

# Subagent 2 salvage — signals, news/sentiment, MCP servers, agent skills (deleg_a93e493d)

The subagent hit the search-guardrail before its summary; below is the salvaged
source trail plus supervisor verification.

## 1. Signals/research approaches for long-term ETF investors
- **Binzhu300/etf-dashboard** — Streamlit "ETF Rotation + Crash-Guard" dashboard: long-only ETF momentum rotation signal visualization, free hosted. Pattern (not stars) is what matters: monthly momentum rank + drawdown guard.
- **vbikkina/tqqq-backtester** — reproducible binary moving-average trend-following backtester; yfinance auto_adjust=True OHLC. Good template for a 1321/2559 200-day-MA crash-guard study.
- Academic baseline: momentum/trend on index ETFs + CAPE-based rebalancing are the classic retail-compatible approaches; no dominant high-star dedicated repo found in this pass (uncertain — search budget exhausted).
- DCA vs lump-sum: consistent literature result is lump-sum wins ~2/3 of the time, but DCA matches NISA monthly accumulation behavior and reduces regret risk. (General knowledge; not re-verified this session.)

## 2. News + sentiment pipelines
- **FinGPT sentiment models** (AI4Finance) — open financial sentiment head models usable for headline scoring.
- **Finnhub** — free tier realtime stocks/forex/crypto API; company fundamentals; now ships an official MCP server integrated into ChatGPT/Claude/Gemini per docs. JP coverage limited.
- **Alpha Vantage** — free JSON/CSV APIs: stocks, ETF, forex, commodities, 50+ technical indicators. JP symbol coverage partial (uncertain).
- **Stooq** — free CSV quotes incl. some JP tickers (uncertain coverage).
- RSS ingestion remains the cheapest JP news path (MarketBrief uses 40+ feeds; swap in NHK/Reuters Japan/Yahoo!Finance Japan/TDnet).

## 3. MCP servers for finance (verified real)
- **shigechika/jquants-mcp** — MCP server for Japanese stock data via J-Quants API v2; **54 tools**: 22 J-Quantents v2 endpoints, 10 market-overview/valuation, 10 offline screener, technical indicators, single-stock summary, equity search/earnings cache tools, chart tools. Docs note Free plan suffices for charts/screener/market-overview; Light covers most retail cases. Small repo (~2★ snapshot) but purpose-built and directly Hermes-compatible via MCP.
- **edinetdb/edinet-db-mcp** — remote MCP server for EDINET filings (Japan FSA disclosure DB); OAuth multi-tenant SaaS with free API tier (EDINETDB_API_KEY). Companion **ai-berkshire-jp** skill (7★) = JP stock investment research using EDINET DB.
- **OpenBB Workspace MCP** (OpenBB-finance/workspace-mcp) — agentic financial workflows over OpenBB; ODP positions OpenBB as "connect once, consume everywhere" infra exposing MCP servers to AI agents. Workspace product has paid tiers; core OpenBB remains OSS (72k★).
- **Finnhub MCP** — official, available inside ChatGPT/Claude/Gemini Enterprise.
- Generic Alpha Vantage MCP servers exist in registries (mcpservers.org) — quality varies (unverified individually).

## 4. Agent skills/plugins ecosystem
- **LLMQuant/awesome-trading-agents** (388★, active 2026-08) — curated LLM trading agents, MCP servers, and agent skills; explicitly lists reusable skills for Claude Code/Hermes Agent/OpenClaw/Codex grounded in LLMQuant data.
- **Tom-roujiang/Awesome-LLM-Quantitative-Trading-Papers** (231★) — paper trail incl. FactorMiner/FactorMAD alpha-mining agents.
- **wilsonfreitas/awesome-quant** (29.1k★, updated daily-ish) — master index of quant libraries by language.
- **georgezouq/awesome-ai-in-finance** (6.4k★) — LLM/DL strategies index.
- 'stockbot'-style agent skills exist on aggregator sites (openagentskill.com etc.) — small/unvetted; treat as reference code only.

## Top-5 picks from this lane (for NISA + Hermes use case)
1. **jquants-mcp** — native MCP into official JPX data; drop-in for Hermes cron research briefs.
2. **edinet-db-mcp (+ ai-berkshire-jp)** — cited fundamental/filing research for Japanese names.
3. **ETF momentum/crash-guard pattern** (etf-dashboard/tqqq-backtester as templates) implemented locally over yfinance .T data — deterministic signals, LLM only narrates.
4. **Finnhub free tier + RSS** — international macro/news context feeding weekly briefs (not JP-primary).
5. **LLMQuant awesome-trading-agents** — ongoing discovery feed for new agent skills/MCP servers rather than an integration itself.
