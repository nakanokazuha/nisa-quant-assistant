# Subagent 3 report — broker/API connectivity, workflows, limitations, J-Quants (deleg_63936684)

Research date: 24 Aug 2026. Star counts are snapshots.

## Executive findings
- Best data source: J-Quants API V2 for Japanese market data and fundamentals; Free tier sufficient for weekly long-term analysis but delayed and rate-limited.
- Best direct broker connection found: Monex, especially its newly announced MONEX MCP Server (Aug 2026) for ChatGPT/Claude account inquiries. Verify scope before relying on it.
- Most broadly compatible broker workflow: export broker CSVs → normalize into SQLite/Portfolio Performance/Beancount → let Hermes analyze the normalized ledger.
- Most usable official trading/portfolio API: kabu STATION API — requires kabu STATION desktop env, same-IP/local-PC operation, Professional/Premium plan eligibility. Awkward for macOS-only Hermes.
- NISA warning: losses in NISA cannot offset gains elsewhere nor be carried forward → automating tax-loss harvesting inside NISA is inappropriate.

## 1. Broker/account connectivity
- Rakuten Securities: no public self-service retail API located (uncertain, not conclusive negative). Fit: 3/10 direct API; 8/10 via CSV/aggregator.
- SBI Securities: no public retail portfolio API located; CSV downloads supported. Fit: 3/10 direct API; 8/10 CSV workflow.
- Monex Securities: OAuth2 Open API (2019) exposes balances/holdings to FinTech providers; Aug 2026 MONEX MCP Server connects ChatGPT/Claude to account info. Verify read-only status, NISA product coverage, freshness, privacy. Fit: 9/10 if Monex user; else 4/10.
- kabu STATION API (Mitsubishi UFJ eSmart): official REST/WebSocket incl. positions/balances; needs Professional/Premium plan, same-IP local Windows-oriented gateway, Japan TZ, one session. GitHub: kabucom/kabusapi (433★), shirasublue/python-kabusapi (17★). Fit 6/10 (3/10 Mac-only).
- GMO Coin: crypto only → 1/10 for this use case.
- Money Forward ME: aggregator with securities linking; no assumed free public API for Hermes. Fit 7/10 as snapshot source; 4/10 as direct API.
- Portfolio Performance: CSV import; broker-export → normalize → PP/SQLite → Hermes narrative. Fit 8/10.
- Beancount/beancount-import: double-entry ledger; engineering-heavy. Fit 7/10 technical users.

## 2. J-Quants API V2 specifics
- Rate limits: Free 5 req/min; Light 60/min; Standard 120/min; Premium 500/min. Exceeding returns 429; sustained excess ≈5-minute block.
- Free plan: 12-week delay, ~2 years history for daily prices/financial summary/earnings dates/listed master. Suitable for weekly/monthly reports, NOT current-price alerts.
- Older plan pricing: Free ¥0; paid ¥1,650 / ¥3,300 / ¥16,500 per month (verify current dashboard).
- SDK: J-Quants/jquants-api-client-python (198★) — ClientV2, API-key auth, pandas DataFrames. Free-level wrappers: listed master, daily prices, financial summary, earnings dates, range utils. Detailed financial statements & dividends under Premium wrappers (get_fin_details, get_fin_dividend) — confirm live plan matrix.
- ETF coverage: daily prices cover 1321/2559 etc.; dividend monitoring may need issuer pages or paid tier. Issuer pages: nextfunds.jp/lineup/1321/, maxis.am.mufg.jp/etf_fund/182559.html.
- Fit: 9/10 weekly research pipeline; 4/10 real-time monitoring.

## 3. Real-world AI workflow examples
- qmyhd/LLM-portfolio-project (2★): brokerage + market + social sentiment journal; SQLite/Postgres, FIFO P/L, LLM summaries. Not Japan-specific. Fit 6/10 design reference; 8/10 if reduced to CSV+SQLite+Hermes.
- yukipanpan/marketbrief (3★): market data + 40+ RSS + macro sources → cited daily briefing via Telegram/Feishu/terminal/JSON; supports data-only mode without AI key (good safety pattern). Adapt US feeds → J-Quants/TDnet/BOJ/JPX. Fit 7/10 after adaptation; 9/10 for no-trade briefing pattern.
- Kingler16/Velora (37★): twice-weekly portfolio briefings via Claude Code, Telegram bot, dashboard, benchmark comparison, outcome tracking. Tax-loss feature inappropriate for NISA → disable. Fit 7/10 after adaptation.
- bauer-jan/stock-analysis-with-llm (70★): AWS Bedrock agents rank stocks weekly; explicit no-real-trades boundary. Fit 5/10 personal; 7/10 architecture pattern.
- AI4Finance-Foundation/FinRobot (7,843★): large financial-agent platform; key principle = deterministic Python computes numbers, LLM narrates with provenance. Heavy stack. Fit 6/10; 8/10 borrowing deterministic-compute/provenance design.
- Earnings-digest pattern: J-Quants summaries + earnings dates + TDnet/IR retrieval → deterministic deltas → short "what changed" memo. Fit 9/10 research; 2/10 autonomous trading.
- ETF distribution reminders (1321/2559): issuer-page watchlist → parse distribution notices → reconcile vs ledger. Fit 8/10 reminder/reconciliation; 1/10 trading signal.

## 4. Honest limitations/risk evidence
- arXiv 2505.07078 (LLM strategies long run): buy-and-hold significantly outperforms LLM timing strategies in bias-mitigated setups; excessive conservatism in bull, poor risk control in bear. → Don't present weekly LLM views as alpha.
- StockBench (arXiv 2510.02209): most LLM agents struggle to beat buy-and-hold in multi-month sequential trading.
- The Alpha Illusion (arXiv 2605.16895): reported LLM-agent alpha often fails temporal-leakage/execution-cost/benchmark checks; not deployment evidence. → Log timestamps, model versions, decisions, costs, passive benchmark.
- FinanceBench (arXiv 2311.11944; patronus-ai/financebench): GPT-4-Turbo with retrieval incorrectly answered/refused a large majority in one config; closed-book worst. → Never let the LLM invent EPS/dividends/P/E/dates/quantities.
- FINRA AI-fraud warning: treat AI recommendations as untrusted drafts; keep credentials out of prompts; read-only access; human confirmation for orders.
- Counter-evidence: ChatGPT stock-selection studies (arXiv 2308.06260; Journal of Financial Economics link) show some usefulness in screening under experimental conditions — not grounds to automate allocation.

## 5. NISA-specific workflow rules
1. Separate accounts in ledger (NISA / 特定口座 / general / cash / FX).
2. No tax-loss harvesting automation in NISA (losses can't offset or carry forward).
3. LLM explains; deterministic code does accounting.
4. Distributions from issuer/JPX sources.
5. Low cadence: weekly/monthly.
6. Human approval gate: vocabulary = monitor / review / rebalance candidate / distribution reminder / missing data — never "buy now"/"sell now".
7. Benchmark everything against a passive index; report turnover, fees, taxes, contribution timing.

## Top five practical integrations (ranked)
1. **J-Quants V2 + Hermes weekly Japanese-market brief** — Free tier → SQLite cache → deterministic metrics → cited LLM summary → Markdown/Discord. Fit 9/10.
2. **Broker CSV → normalized portfolio ledger → Hermes reconciliation** — works without any public API; auditable. Fit 9/10.
3. **Monex MCP Server** (if Monex user) — read-oriented account checks; verify permissions/privacy. Fit 9/10 Monex; 0/10 otherwise.
4. **ETF distribution and earnings-date monitor** — issuer pages + J-Quants earnings dates → ex-date/payment-date/missing-distribution alerts. Fit 8/10.
5. **Cited, no-trade cron briefing** (MarketBrief/Velora pattern adapted to JP) — disable auto BUY/SELL and tax-loss modules. Fit 8/10 after adaptation.

Not recommended first: kabu STATION API unless already an MUFG eSmart customer on qualifying plan with Windows sidecar.

## Environment issues encountered
- Browser blocked by Chrome remote-debugging permission prompt; used web search + HTTPS instead.
- GitHub API rate-limited; verified stars from repo HTML.
- Rakuten/SBI API status uncertain (absence of documentation ≠ proof of nonexistence).
- J-Quants plan entitlements change over time; recheck docs before implementation.
