# Hermes MCP integration

This repository exposes the existing Phase 3 NISA quant producer through a
small local stdio MCP server. It is advisory and read-only: it has no broker,
order, trading, arbitrary-command, arbitrary-path, SQL, or LLM-prediction tool.

## Add the server to Hermes

Run this exact command from the Hermes environment:

```bash
hermes mcp add nisa_quant \
  --command /Users/user/.hermes/hermes-agent/venv/bin/python \
  --args /Users/user/Documents/Yume/nisa-research/tools/nisa_quant_mcp_server.py
```

The equivalent `~/.hermes/config.yaml` entry is:

```yaml
mcp_servers:
  nisa_quant:
    command: /Users/user/.hermes/hermes-agent/venv/bin/python
    args:
      - /Users/user/Documents/Yume/nisa-research/tools/nisa_quant_mcp_server.py
```

Adding or changing the server requires a normal Hermes restart or a new
session for tool discovery. The server is started by Hermes over stdio; it
must not be launched through a shell wrapper that adds commands or paths.

## Tools and safe modes

- `nisa_quant_refresh` accepts `as_of`, `start`, `end`, `mode`, optional
  `limit`, optional non-secret `sec_contact`, and `format`.
- `nisa_quant_latest` reads the latest fixed Hermes report and manifest and
  returns bounded report content, or a structured `not_yet_run` result.

`mode` defaults to `replay`. Replay reads only the fixed
`data/phase3` cache directory and makes no network request. `mode: "live"`
explicitly permits the existing producer's public read-only provider calls.
`limit` is only for an explicit small live smoke and is bounded to 50; use
`limit: 3` for the smallest smoke. `sec_contact` is only accepted in live
mode and must be a short non-secret contact string. `format` is `markdown` or
`json`, writing only `reports/phase3/hermes-report.<format>` plus its adjacent
manifest.

Example Discord prompts:

```text
Use nisa_quant_latest and tell me whether the latest NISA quant refresh is available. Include the status, prediction tickers, freshness, and any gaps.
```

```text
Run nisa_quant_refresh for as_of 2026-09-16, start 2024-01-01, and end 2026-09-16 in replay mode with JSON format. Do not use live network access.
```

```text
Run an explicit live smoke for as_of 2026-09-16, start 2024-01-01, and end 2026-09-16 with mode live, limit 3, and a non-secret SEC contact string. Report any unavailable result truthfully.
```

## Output and status interpretation

Each refresh result includes the fixed report and manifest paths, exit code,
status, current prediction count and tickers, freshness summaries, SEC status,
performance-claim suppression, gaps, failures, and a concise summary.

- `available` and `available_descriptive` are completed exit-0 reports.
- `unavailable_insufficient_data` is a completed structured result with exit 2;
  it is not an MCP transport failure and publishes no performance claim.
- `unavailable` is a completed producer result with explicit failure evidence.
- `validation_error` means the producer was not called.
- `not_yet_run` means no fixed latest Hermes report exists.

Current predictions are descriptive research rankings only. They are not
recommendations, calibrated probabilities, or evidence of future
outperformance. Current-survivor runs suppress historical performance claims;
freshness, SEC coverage, gaps, and provider failures remain explicit.
