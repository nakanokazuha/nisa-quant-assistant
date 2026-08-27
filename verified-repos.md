# NISA AI Integration Research — working data (verified GitHub API, 2026-08-23/24)

## Verified repo stats (GitHub API, stars / language / last push)

| Stars | Repo | Lang | Last push | Notes |
|---|---|---|---|---|
| 99,531 | TauricResearch/TradingAgents | Python | 2026-07-18 | Multi-agent LLM trading framework (analysts→trader→risk debate) |
| 72,221 | OpenBB-finance/OpenBB | Python | 2026-07-30 | Open data platform for analysts/quants/AI agents |
| 63,011 | virattt/ai-hedge-fund | Python | 2026-08-07 | AI hedge-fund team of agents; educational |
| 53,565 | freqtrade/freqtrade | Python | 2026-08-24 | Crypto bot (out of scope for NISA) |
| 47,879 | microsoft/qlib | Python | 2026-07-23 | AI quant platform (ML/RL), pairs with RD-Agent |
| 29,100 | wilsonfreitas/awesome-quant | HTML | 2026-08-24 | Master curated list |
| 25,062 | ranaroussi/yfinance | Python | 2026-08-20 | Yahoo Finance data incl. JPX .T tickers |
| 22,936 | backtrader/backtrader | Python | 2024-08-19 | Backtesting; maintenance stalled |
| 21,139 | AI4Finance-Foundation/FinGPT | Jupyter | 2026-08-02 | Financial LLMs, sentiment |
| 20,594 | stefan-jansen/ml-for-trading | Jupyter | 2026-08-23 | ML for Trading book code, active |
| 20,061 | quantopian/zipline | Python | 2024-02-13 | Archived-ish; community fork zipline-reloaded |
| 16,076 | AI4Finance-Foundation/FinRL | Jupyter | 2026-07-13 | Deep RL trading; research-grade |
| 14,321 | microsoft/RD-Agent | Python | 2026-08-04 | Automated factor/model R&D on qlib |
| 12,388 | goldmansachs/gs-quant | Python | 2026-08-17 | Institutional toolkit; needs GS credentials |
| 8,795 | polakowo/vectorbt | Python | 2026-08-02 | Fast vectorized backtesting |
| 8,369 | jesse-ai/jesse | Python | 2026-08-19 | Crypto-focused framework |
| 6,430 | georgezouq/awesome-ai-in-finance | — | 2026-08-04 | Curated list |
| 388 | LLMQuant/awesome-trading-agents | — | 2026-08-13 | LLM trading agents/MCP/skills list; mentions Hermes Agent skills |
| 231 | Tom-roujiang/Awesome-LLM-Quant-Trading-Papers | — | 2026-07-24 | Papers list |
| ~0* | obichan117/pyjpx-etf | Python | 2026-07-28 | JPX ETF PCF data lib (*new/small) |

## Key facts from search
- TradingAgents v0.3.1 (2026-07): look-ahead filtering fixes, checkpoint resume, retry budget. Paper: arXiv 2412.20138.
- ai-hedge-fund (virattt): educational agent team; uses yfinance-compatible data; US-focused but tickers pluggable.
- OpenBB: repositioned as "Open Data Platform… for AI agents" — has platform + MCP-style agent integration path.
- Qlib ~44k stars per mid-2026 coverage; RD-Agent automates factor/model evolution ("AI hedge fund that writes its own factors").
- pyjpx-etf: JPX ETF portfolio composition (PCF) fetcher — directly Japan-relevant, small/new.
- J-Quants API (JPX official): free tier exists; needs verification of limits (subagent task).
- Yahoo Finance supports Tokyo tickers with `.T` suffix via yfinance (e.g., 7203.T Toyota, 1321.T Nikkei225 ETF).
