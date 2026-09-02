# Phase 2 source registry and operating limits

This registry is the active source status for the standalone Phase 2 evidence
layer. It is deliberately separate from the historical Japan-first research
artifacts. No source is silently substituted when a configured source is
unavailable.

## Universe: S&P 500 membership

S&P Dow Jones Indices describes the S&P 500 as an index of 500 constituent
companies and publishes its US indices methodology:

- index page: <https://www.spglobal.com/spdji/en/indices/equity/sp-500/>;
- methodology: <https://www.spglobal.com/spdji/en/methodology/article/sp-us-indices-methodology/>.

The repository does not scrape or silently treat the current web page as
historical truth. `phase2_universe_members` requires a supplied CSV row with a
`universe_id`, `effective_date`, source URL/version, UTC-normalized retrieval
time, `lookahead_bias_status`, and `survivorship_bias_status`. `point_in_time`
and `current_snapshot_only` are distinct labels. A current-only snapshot is
never presented as historical membership. The checked-in universe fixture is a
synthetic subset for tests, not a claim that it is the complete S&P 500.

## Market observations

Alpha Vantage documents `TIME_SERIES_DAILY` as raw daily open, high, low, close,
and volume data, with compact/full output-size limits and an API key:

- <https://www.alphavantage.co/documentation/#daily>.

The adapter is implemented but configuration-required. The key is supplied by
the caller, is not written to SQLite/logs/citations, and full-history access
may require a paid plan according to the provider documentation. The default
test path injects local observations through the same provider-neutral
interface. Market rows are daily OHLCV only; no real-time claim is made.

## SEC EDGAR filings and facts

The SEC documents public unauthenticated JSON APIs for submissions and XBRL
Company Facts at `data.sec.gov`:

- API documentation: <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>;
- developer FAQ, including User-Agent, 10 requests/second maximum, filing lag,
  ticker/CIK mapping caveats, and RSS: <https://www.sec.gov/about/webmaster-frequently-asked-questions>;
- Section 16 forms: <https://www.sec.gov/file/edgarfm-vol2-v76>.

The adapter accepts explicit CIK/ticker mappings and only supplied JSON fields.
It records accession, filing/publication date, report period, fact/field,
citation, retrieval time, source version, and uncertainty. Forms 3/4/5,
Schedules 13D/13G, and 13F-HR are represented as alerts with their actual form
names. Publication lag is retained; it is not converted into an event date or
an investment conclusion. The adapter does not infer fundamentals or parse
unverified endpoints.

Live SEC setup requires a descriptive User-Agent supplied outside this
repository, a bounded HTTPS allowlist, and respect for the documented access
limit. The implementation uses a local limiter consuming the configured
`max_requests_per_second` value before each SEC request; it does not claim a
provider-side capability beyond the documented limit. No credentials are required by the documented data APIs, but live
retrieval is not claimed by fixture tests.

`phase2-refresh-config` wires submissions and Company Facts for each latest,
cutoff-eligible active universe state using that row's explicit ticker/CIK pair.
A payload whose
accession CIK does not match the target, or whose RSS aliases map to zero or
multiple tickers, is rejected and audited. The command does not choose the
first issuer in the database.

Company Facts requires a valid top-level `cik` that normalizes to the
explicitly requested CIK; missing, malformed, or mismatched CIK values are
rejected without attribution. An absent `accn` is not invented: the fact
remains attributable only through the explicit mapping of requested CIK,
taxonomy/fact, unit, period, and retrieval metadata, and its source identifier
is a deterministic fact/period identity rather than a fabricated accession.

## News/RSS

The SEC developer FAQ confirms RSS feeds for some EDGAR searches. The generic
RSS adapter also supports a source explicitly configured by the user. Each
source must provide a feed URL, terms URL, source version, explicit ticker/entity
aliases, and either `metadata_only` or `bounded_excerpt` content policy. The
adapter stores at most a bounded title/summary excerpt and never bypasses a
paywall or assumes a license. Unmapped or multiple ticker matches become an
audit failure. Article identity uses the feed GUID/link, so replays do not
duplicate articles.

## Watch-alert evidence

SEC filing alerts support Forms 3/4/5, 13D/13G, and 13F-HR metadata. Large-flow
proxies are accepted only when a configured source names one of
`short_interest`, `unusual_volume`, or `options_activity`; the persisted topic
is `large_flow_proxy` and the observable field is retained. `whale_activity`,
generic `flow`, and unsupported proxy names fail closed. These records are
evidence only and are not a trading signal.

## Freshness, conflicts, and failures

All observation/publication/retrieval timestamps are normalized to UTC. A
retrieval before the observation/publication date is rejected. Current and
observed market rows can enter an evidence snapshot; stale, unavailable, or
conflicting data remains an explicit failure/audit state and cannot silently
become a current fact. Provider errors, malformed payloads, partial OHLCV,
ambiguous mappings, unsupported forms/proxies, and source conflicts are stored
in `phase2_failures` with request identity. Exact provider observation and
article identities are idempotent and safe to replay.

Configured Alpha Vantage and SEC endpoints are validated against fixed official
provider hosts before any request; configured values cannot expand the secret
transport trust boundary. RSS may use an explicitly configured public host,
but it receives no provider secret and IP-literal, loopback, link-local, and
credentialed references are rejected. Redirects are disabled. Market freshness
is checked against the observation date and normalized retrieval instant, so
an impossible `current` label is downgraded to `stale`. A configured refresh
is `failed` when no configured provider returns at least one validated,
non-stale, non-conflicting observation or evidence record for the current
response. Replaying an already stored usable record still counts as usable;
insertion count is not the success signal. `completed_with_warnings` is
reserved for runs where at least one configured provider did complete with
usable data.

## Manual setup

Copy `config/phase2-sources.example.json` to a private local configuration if
needed. Keep API keys and private/licensed URLs outside the repository. The
fixture commands require no secrets and are the reproducible acceptance path.
The configured command is read-only and usable with an injected transport in
tests; its live network proof remains configuration- and environment-dependent.
The live capability is therefore configuration-dependent, not fixture-proven.
