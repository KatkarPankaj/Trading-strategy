# Platform operations

## Research the full verified universe

The autonomous research and paper-cycle APIs, CLI, and US worker now default to
all eligible instruments in the configured universe. `PAPER_AUTO_LOOP_TOP_N`
and `--top` remain optional ways to request a smaller research subset. Do not
set `AUTONOMOUS_RESEARCH_SETTINGS.max_candidates` or
`CANDIDATE_RESEARCH_SETTINGS.max_candidates` unless an intentional research
ceiling is required. Research concurrency remains bounded by existing settings;
the number of simultaneous positions, order sizing, cash, exposure, and all
other risk controls are unchanged.

The US universe currently contains ten verified members, so this change does
not add stocks. Expand the canonical instrument master only after reviewing
instrument identity/eligibility and provider request budgets. Researching more
members can increase market-data, news, and AI requests; do not assume provider
capacity or cost is unlimited. Never alter `MAX_POSITION_QUANTITY` to change
research coverage; it is a per-order share limit.

Before increasing runtime throughput, review provider credits/rate limits, AI
budgets, cycle duration and the previously observed protective-monitoring gaps.
Keep protective quote capacity reserved. No cadence, concurrency, strategy
threshold, portfolio limit, provider entitlement or operator runtime control is
changed by installing this source update. A separately approved worker restart
is needed to activate source/configuration changes.

## India cash-equity calendar observations (2026, unreviewed)

`src/stockmarket/core/calendars/XBOM.json` and `XNSE.json` contain the 16
weekday dates separately observed in the official BSE and NSE 2026 equity
holiday tables on October 30, 2026:
[BSE](https://www.bseindia.com/static/markets/marketinfo/listholi) and
[NSE](https://www.nseindia.com/resources/exchange-communication-holidays).
The observed date sets match; that does not make the exchange calendars
interchangeable. Each calendar remains a separate unreviewed observation, and
its `source` metadata is informational, **not** the loader's `review`
authorization contract.

Both pages announce November 8 Muhurat trading but say timings will be notified
separately. No hours are inferred or copied from other segments. No independent
review, verified special-session schedule or 2027 coverage is claimed. Without
the required reviewed evidence artifacts/report for each exchange, both
calendars remain closed even on ordinary weekdays. India funding, feed, mapping,
risk and single-market worker gates remain unchanged.

### Kite REST usage guard (Phase B)

The Phase-B ledger/controller records application-tracked REST request attempts
in the `kite_rest_usage` table. It does not claim that a tracked request equals
a Zerodha billing credit: the account-specific meaning of the reported
"500 credits" remains unverified. `KITE_MONTHLY_CREDIT_BUDGET` is therefore an
application cap, not an estimate of remaining provider credits. The default
policy is 300 tracked requests in a rolling 30-day window, warnings at 50%, 70%
and 85%, and a nonessential-request stop at the lower of the configured hard
stop and the reserve boundary. The reserved capacity is limited to essential
authentication/recovery calls. The window rolls by request timestamp; it does
not reset on a calendar-month boundary.

Configure `KITE_WARNING_THRESHOLDS`, `KITE_HARD_STOP_PERCENT`,
`KITE_RESERVE_PERCENT`, and `KITE_USAGE_TRACKING_ENABLED` as shown in
[`.env.example`](../.env.example). If tracking is disabled, guarded requests are
rejected. Only the read-only market-data endpoints and the authentication
session endpoint are admissible through the controller; order routes are
rejected. The ledger records endpoint path and purpose, but never credentials,
query strings, response bodies, or raw exception text.

This phase adds the ledger and admission controller only. It does not create a
Kite client, make API requests, enable India execution, or wire provider calls
through the controller. Migration 28 must be explicitly applied before using
the repository; do not apply it to a running account without the normal backup
and migration review. WebSocket, instrument-master, historical-data, and
dashboard integration remain future phases.

### Kite authentication and instrument-master cache (Phase C)

The local Kite client is dormant until explicitly constructed and called. Set
`KITE_API_KEY_FILE` to the local API-key file (the `.env.example` points to the
existing `secrets/zerodhasecret.txt` file) and set `KITE_API_SECRET_FILE` to a
separate file containing the API secret. The API key and API secret are distinct
credentials; do not put either value in `.env` or source control. The client
can return the browser-login URL, exchange a user-supplied request token once,
and atomically save the resulting access token to `KITE_ACCESS_TOKEN_FILE`.
Keep the `secrets/` directory restricted to the local user; the token is not
stored in the database or emitted in logs.
Authentication and instrument downloads each pass through the persistent
request guard. There are no retries or background login requests.

The full instrument CSV and a checksum/timestamp sidecar are cached under
`outputs/kite/`. A cache is reused for 24 hours; an expired or absent cache
causes one guarded master request when explicitly requested. Unreadable,
partial, mismatched, or future-dated cache metadata fails closed. NSE and BSE
rows retain Kite's exchange, trading symbol, token, segment, expiry, lot size,
tick size, and instrument type. The cache is provider reference data only: it
does not validate an instrument mapping, map tokens into the canonical
execution master, certify exchange entitlement, or enable India orders.
Current calendar, provider, mapping, and research-only readiness gates remain
unchanged. No access-token exchange or instrument download occurs at startup.

The October platform is paper-trading software, not a live execution service or a
production-readiness claim. Keep `TRADING_MODE=paper` and
`ENABLE_LIVE_TRADING=false`. The checked-in Compose stack pins those values.
Do not expose the API directly to the public internet; the provided Compose
binding is loopback-only and remote access requires a separately secured TLS
terminator and network policy.

## Local API

For the reviewed `afc1623` follow-up, see the
[US migration preflight and acceptance procedure](PAPER_MIGRATION_ACCEPTANCE.md).
Its operator commands are approval-gated instructions, not completed actions.

### Dashboard/API mismatch and supervised paper-test preflight

The dashboard requires the shared `/health.dashboard_contract`: version `1`,
with `market_watch`, `paper_market_readiness`, and `execution_data_status`.
Missing/malformed/incompatible contracts show read-only health diagnostics and
withhold interactive dashboard controls. This does **not** pause or stop a
server-side loop that was already enabled. Compatibility is not risk approval:
a compatible but degraded API remains blocked by its trading prerequisites.
Missing Market Watch/readiness routes report a capability mismatch rather than
an empty list of markets. A successful health response alone does not prove
that the dashboard and API support the same features.

Do not run a replacement worker alongside the existing account. Resolving a
running old API requires a separately authorized controlled shutdown/restart
using matching code and the intended environment. Before doing that, confirm
the configured database schema with the existing schema-status procedure below,
review backups/recovery, and leave automation disabled during startup validation.
Do not apply migrations implicitly to the operator database, reset cash, switch
the account's market, or delete its ownership sidecar to work around a blocker.
The VS Code `Start US Paper Auto-Loop API` task sets `AUTO_MIGRATE=false`;
pending schema changes must be reviewed, backed up and explicitly applied before
starting it. Automatic-entry pauses are restored from the persisted audit
control, independently of whether the scheduling loop is running.

`/health.execution_data_status` distinguishes missing/stale data during the
execution worker's regular session from missing/stale data outside that session.
It retains original source timestamps/ages, never calls a provider, and never
removes `MISSING_OR_STALE_EXECUTION_DATA` from readiness blockers. Closed-market
prices are not a reason to overwrite timestamps, use zero prices, or silently
switch to India. On 2026-10-14, the US normal opening is 13:30 UTC / 19:00 IST
(09:30 America/New_York); use the configured reviewed calendar rather than this
one-date example for scheduling. A pre-open stale observation is not proof of
a broken feed, but it is not a usable entry quote either.

Before a supervised US paper test, after separately approved service alignment:

1. Confirm PAPER mode, the intended US worker identity, an unchanged USD account,
   compatible dashboard contract, and the opt-in loop still disabled/paused.
2. Verify completed recovery, healthy independent protective monitoring, entry
   pause/kill-switch controls and no unexpected positions/pending orders.
3. During a verified US session, validate actual provider entitlement, source
   identity/timestamps/freshness, instrument mapping and limits. A closed circuit
   alone is not feed validation. Use the existing bounded quote/order workflow;
   never refresh data by changing timestamps or treat a status GET as a quote.
4. Review conservative paper limits and monitor a small, supervised session.
   A `COMPLETE` cycle or `RUNNING` loop is not evidence of a fill or profitability.

India cannot be unblocked by this software preflight alone. Obtain official
cash-equity calendar publications and review every date/session using the
artifact contract below; independently validate an execution-quality India feed,
mapping, entitlement, delay, tick/lot/quantity rules and cost assumptions. Current
adapters have no such approval path. An isolated INR account/configuration needs
separate authorization; the existing USD balance must never be relabeled as INR.
No startup, funding, feed promotion, calendar certification or order action is
performed by opening the dashboard.

### India readiness and single-market worker boundary

`PAPER_EXECUTION_MARKET` binds one process to one market; absent that setting,
the explicit `PAPER_AUTO_LOOP_MARKET` or existing first-market default is used.
Both explicit settings must agree and be enabled by `MARKETS`. An automatic
loop's universe definition **and all members** must belong only to that market.
Market Watch selection changes observation only. `/health` evaluates the worker's
session, not whether any observed market happens to be open. Startup structured
logs, `/health`, and authenticated `GET /paper/market-readiness` expose the scope.
Paper automation remains opt-in; live execution and model training are unchanged.

The startup audit event `PAPER_WORKER_BOUND` persists market, base currency and
initial cash seed. Restart refuses changes to that identity or foreign-market
order history. Existing US/USD histories retain their existing default seed;
unbound non-USD histories with fills need a separately reviewed account migration.
No history is deleted or cash reinterpreted. New India workers require
`BASE_CURRENCY=INR` and explicit `PAPER_STARTING_CASH`. Positive available INR cash
is also rechecked on entry. `FX_RATES` is rejected for India workers: static,
untimestamped rates and display conversions are not a supported funding policy.
No funding route or cross-currency conversion was added.

Each API bootstrap (including personal research, which performs recovery) holds
an OS-owned sidecar lease next to its local SQLite file. A second bootstrap on
that file fails; normal application shutdown, database close, startup failure,
or process termination releases ownership. Never delete/replace the sidecar
while a worker is active. Use the normal ASGI shutdown path: monitoring must stop
before providers/ownership are released. A stuck trading/kill-switch monitor
causes shutdown to fail and retain ownership. Timeout does not cancel work.
Direct `build_context` callers must close providers and release `worker_lease`,
or close the database, after stopping work. In-memory databases are test-local
and do not provide cross-process ownership. This is not distributed locking:
hard-linked aliases, network filesystems, copied databases, raw repository
writers and separate databases targeting the same external account are outside
the contract. PostgreSQL guarded recovery/worker ownership is not supported.
Simultaneous US/India execution is **not integration-certified**; separate files,
paper accounts, currency seeds, calendars and provider budgets are prerequisites,
not proof of simultaneous readiness.

India readiness separately reports observation, reviewed calendar, instrument
metadata, independently verified provider mapping, provider validation, explicit
INR funding, completed replay/clean reconciliation, worker scope and session.
The ordinary entry submission gate checks these again under the execution lock;
protective exits retain their existing risk/quote/idempotency checks and are not
disabled by an entry-readiness failure. Health/status GETs never query providers,
resume recovery, or approve risk. Missing/invalid quotes still use existing
source/identity/timestamp, tick/lot, sizing and final RiskEngine checks.

**Current blockers:** no independently verified India execution feed, mapping
attestation, entitlement/delay/rate-limit approval, or transaction-cost policy is
implemented. Yahoo, Twelve Data and mock remain research-only. There is no
environment boolean that promotes them. Accordingly `provider_validated` and
`instrument_mapping_verified` remain false and **India entries remain blocked**,
even with INR cash and fresh observations.

#### Reviewed India calendar artifact contract

India has no bundled verified calendar. Separate `XNSE.json` and `XBOM.json`
are required; neither an `IN.json` fallback nor an empty holiday list establishes
reviewed coverage. Official candidate source indexes reviewed on 2026-10-14:

- NSE: <https://www.nseindia.com/resources/exchange-communication-holidays>
  and <https://www.nseindia.com/api/holiday-master?type=trading>.
  Requests timed out; no publication contents were verified.
- BSE: <https://www.bseindia.com/static/markets/marketinfo/listholi.aspx>
  failed transport; <https://www.bseindia.com/markets/MarketInfo/DispNewNoticesCirculars.aspx>
  returned only a notice-table heading, not a verifiable schedule/circular.

These are discovery indexes, **not verified publications for any enabled date**.
Obtain the exact official cash-equity holiday and special-session circulars.
Archive the original bytes locally; a human reviewer must compare **every**
holiday, early close and special-session interval with publication/page evidence.
Do not infer Muhurat timings, holidays, or an entire year's coverage.

Venue JSON retains `years`, `holidays`, `early_closes`; `special_sessions` maps an
ISO date to ordered, non-overlapping local `[open, close]` time pairs. Explicit
special sessions can open an otherwise closed weekend/holiday; gaps and exact
close boundaries remain closed. No timing is supplied implicitly.

The `review` object requires `exchange` (`XNSE`/`XBOM`), `segment=CASH_EQUITY`,
aware `retrieved_at` and `reviewed_at` (ordered and not future),
`coverage_start`, `coverage_end`, `expires_on`, `sources` and `validation_report`.
Each source has official exchange HTTPS `url`, relative local `artifact` and
`sha256`. The report contains:

- `status=REVIEWED`, named `reviewer`;
- `schedule_sha256`: SHA256 of UTF-8
  `json.dumps(schedule, sort_keys=True, separators=(",", ":"))`, where `schedule`
  contains exactly `years`, `holidays`, `early_closes`, `special_sessions`
  (missing optional maps default to `{}`, holidays to `[]`);
- `verified_dates`: sorted unique union of all event dates;
- `date_evidence`: exactly those dates, each with `source_sha256` matching an
  archived publication and a nonempty publication/page/paragraph `locator`.

Reproduce integrity validation offline with the configured interpreter:

```text
python -c "from pathlib import Path; from stockmarket.core.markets import load_calendar_file; load_calendar_file(Path('reviewed-calendars/XNSE.json'), reviewed_exchange='XNSE'); load_calendar_file(Path('reviewed-calendars/XBOM.json'), reviewed_exchange='XBOM')"
```

Set `PYTHONPATH=src` and use only the reviewed artifact directory.
This verifies artifact/schedule integrity and review completeness, **not the
truth of a publication or reviewer assertion**. No automatic PDF interpretation,
exchange authenticity signature, or calendar-update subscription exists.
Runtime rejects absent/unreviewed/expired/out-of-bound coverage. Schedule or
artifact changes require a new report/review and controlled restart; no hot
reload or update is performed. Synthetic tests prove gating, not official dates.
Acceptance for real India scheduling remains incomplete until actual official
publications and independently validated execution inputs are available.

### Market Watch: US and India observation slice

The dashboard sidebar now offers **Market Watch** separately from Trading Monitor.
Set `MARKETS=US,IN` to expose both configured markets. The page uses the same
canonical `Instrument` objects, market registry and imported master as research
and execution; it does not discover or invent listings. Import a reviewed
`INSTRUMENT_MASTER_FILE` through the existing bootstrap workflow. The development
master remains recommendation-only and is not eligible for paper execution.
Existing stored metadata is preserved. No configuration is changed automatically.

V25 adds `market_watchlist`, referencing canonical instrument IDs. It is one
shared authenticated-operator watchlist, not a multi-user authorization model.
Back up and review the existing explicit migration procedure before installing
this version; this implementation only exercised migrations in disposable test
databases. Never point development tests at the operator database.

| Authenticated route | Behavior |
|---|---|
| `GET /market-watch/markets` | Configured markets, calendar coverage, sessions, provider and freshness policy |
| `GET /market-watch/instruments?market=IN&q=RELIANCE&limit=50&after=...` | Canonical symbol/name search, bounded to 100, deterministic exclusive ID cursor |
| `GET /watchlist?market=IN` | Persisted membership and cached quote observations; no provider reads |
| `POST /watchlist` | Add `{"instrument_id":"XNSE:RELIANCE"}` idempotently |
| `POST /watchlist/remove` | Remove the same canonical identity idempotently |
| `POST /market-watch/refresh?market=IN` | Explicit provider reads; never submits an order |

The global watchlist limit is 100; explicit refresh supports at most 10 members
per market and one in-flight refresh per market. A five-second soft work budget
stops admitting additional reads, and a read can take up to its two-second worker
wait beyond that budget. Incomplete results are explicit. There is no background
price-fetch loop. GET recomputes freshness from the original source timestamp;
reloading a browser never makes an old quote fresh.

Prices are null when unavailable/invalid, never zero defaults. A structurally
valid cached observation may be displayed as STALE with its original price and
timestamp, but is never an execution input. Failed refresh clears the cached
usable price and reports an error code. Quotes include source time, retrieval
time, age, currency, exchange identity, tick/lot sizes, type and session.
Mock observations are explicitly SYNTHETIC; other source names are UNATTESTED,
not independently verified real-feed provenance. Watchlist membership and a
FRESH display are not strategy signals or RiskEngine approvals.

Each market has a separate bounded watch worker pool and circuit breaker,
separate from execution/research pools. Timeout does not cancel a running vendor
call; its capacity stays charged until actual completion. Late results cannot
publish into the observation cache after a timeout. Vendor global locks, Python
native work and shared account quotas can still couple markets. Watch and
execution clients do not coordinate a distributed vendor-wide quota; allocate
separate credentials/budgets or conservatively budget their combined requests.
Watch work has no trading lock, but watchlist persistence shares SQLite admission.

Provider selection reuses `DATA_PROVIDER_US` / `DATA_PROVIDER_IN` routing.
`YAHOO_SYMBOL_MAP` and `TWELVEDATA_SYMBOL_MAP` map existing canonical IDs to
vendor aliases without rewriting instruments. NSE defaults to Yahoo `.NS`;
BSE `.BO` often requires an explicit verified vendor code, for example a numeric
listing code rather than an NSE symbol. Twelve Data includes venue identity and
validates response metadata. These sources remain research-only; plan entitlement,
exchange coverage, quote delay and listing validity require independent checks.
The India personal-research launch task explicitly selects Twelve Data and stops
if its configured key file is missing; this provider choice does not imply NSE/BSE
entitlement, verified instrument mappings, or permission to execute paper orders.
Optional `TWELVEDATA_API_KEY[_FILE]_IN` / `_US` and
`TWELVEDATA_REQUESTS_PER_MINUTE_IN` / `_US` scope credentials and local pacing.
Scoped key files do not inherit a global inline key. No credentials are returned.
The default provider must still have its required credentials; scoped values
override a configured route rather than inventing an unconfigured default.

`MARKET_WATCH_MAX_AGE_SECONDS_IN` and `_US` set watch freshness, must be positive,
and cannot exceed the existing execution maximum. These display policies do not
change or relax execution risk/freshness rules. Multi-currency paper accounting
already requires explicit FX and currency cash: USD cash is not silently converted
into INR, and a USD-funded account is not an India-funded account. No funding or
FX mutation route was added. Instrument eligibility, local entry windows,
deterministic sizing, session/freshness and final risk/order checks stay on the
existing execution path.

#### Independent calendar configuration and remaining execution limitation

`MARKET_CALENDAR_DIRECTORY` replaces the bundled directory. Supply reviewed
`US.json` and separate reviewed `XNSE.json` and `XBOM.json`; optional US MIC
files `XNYS.json`, `XNAS.json`, `ARCX.json` override `US.json`. JSON uses the existing
`years`, `holidays`, `early_closes` format, with the India review contract above.
Missing US venue files fall back to the US country file; India requires separate
reviewed venue files. Missing/uncovered data fails closed. Do not mark a year
covered using a fictitious empty holiday list. Retain source/verification metadata
with the reviewed files. US daylight-saving conversion uses America/New_York;
India uses Asia/Kolkata. Normal hours remain explicit market definitions.

Venue-specific Market Watch phase respects each calendar. Country-wide execution
checks conservatively require venues to agree and use the earliest venue close
for scheduled flattening. This can intentionally withhold entries on differing
venue days. Special weekend sessions, Muhurat trading and exceptional sessions
are not modeled by this calendar format and are not claimed supported.

India's official holiday endpoints could not be retrieved during this change.
No purported verified Indian holiday calendar was bundled. Without reviewed IN/
venue files, India can be searched/watched but session-authorized execution stays
closed. US bundled coverage remains unchanged.

The automatic paper loop still schedules **one** explicitly selected
`PAPER_AUTO_LOOP_MARKET` / universe per process and reports
`scheduling_scope=SINGLE_MARKET`. Watch selection does not change it. Do not start
two API processes against one paper account to simulate multi-market scheduling.
Simultaneous multi-market automation, account-funded INR admission and independent
execution-feed failure budgets need a later coordinated execution milestone.
Existing global entry/risk limits remain in force; no new order path, live
execution, automatic FX conversion or AI training/promotion was added.

1. Install the dependencies listed in `requirements.txt` in a Python 3.10+
   environment.
2. Set the required risk-window and order-limit environment variables described
   in `.env.example`. Use a local SQLite file for the validated paper startup
   path. A PostgreSQL SQL adapter exists, but guarded history replay currently
   refuses its unsupported snapshot-revision contract; that deployment is not
   certified. Credentials belong in environment variables or secret files,
   never in committed configuration.
3. Set a strong `API_TOKEN` before using protected endpoints. Run
   `PYTHONPATH=src python -m stockmarket.ops check-config` to validate settings.
4. Apply migrations explicitly with `PYTHONPATH=src python -m stockmarket.ops
   migrate`, then start
   `uvicorn stockmarket.api.bootstrap:create_app_from_env --factory --workers 1`.
   Keep one worker: order, portfolio and trading-gate state are process-local.

The default API bootstrap does not install an AI provider, research strategy
registry, or explicit research market-session map. Consequently `POST
/research` remains unavailable (503) until configured. Configure an
OpenAI-compatible endpoint with `AI_BASE_URL` (base URL ending in `/v1` where
applicable), `AI_MODEL`, and `AI_API_KEY` or `AI_API_KEY_FILE`. The endpoint
must use HTTPS; HTTP is accepted only for loopback development.
For `api.openai.com`, advisory requests use strict JSON Schema structured
outputs derived from the task's Pydantic schema. Use a model supporting
structured outputs (the local setup uses `gpt-4.1-mini`). All object fields
are required on the wire; nullable fields may be null. Local validation still
checks ranges, identities, citations and cross-field rules. Unsupported models
fail closed; requests never silently retry using weaker JSON mode.
Other OpenAI-compatible endpoints retain JSON mode and local strict validation
because their structured-output support has not been established.
The Opportunities tab's `/intelligence/opportunities` workflow additionally
requires a news provider: configure `NEWS_PROVIDER=finnhub` and
`FINNHUB_API_KEY` or `FINNHUB_API_KEY_FILE`, then restart the API. OpenAI and
Twelve Data keys do not provide Finnhub news access. Without this configuration,
that feature returns 503; the separately configured paper auto loop can still
run and reject candidates for missing evidence or absent deterministic signals.
The local "Start US Paper Auto-Loop API" task reads the Finnhub key from the
ignored `secrets/finhub-api-key` file. The key is never embedded in the task.
Dashboard advisory research and opportunity ranking use a bounded 300-second
read timeout for provider and AI processing; other requests retain the normal
10-second timeout. A read timeout does not cancel server-side processing and
does not trigger an automatic retry. Check run/order status before resubmitting.
`AI_TIMEOUT_SECONDS` defaults to 20 and is bounded to 1-120 seconds.
Optional cost controls: `AI_MAX_RETRIES` (0-3, default 0) retries only
timeouts and HTTP 408/429/5xx; `AI_TOKEN_BUDGET` caps per-process token use
and fails closed with `AI_BUDGET_EXHAUSTED` (it resets on restart);
`AI_MAX_OUTPUT_TOKENS` (1-32768, default 4096) is sent as `max_tokens`, which some reasoning
models reject. Each persisted assessment records `ai_usage` (calls, attempts,
prompt/completion counts, and whether counts were estimated).
`AI_CONTEXT_WINDOW_TOKENS` (1-1048576, default 32768) is a conservative
application cap, not automatic detection of the selected model's actual window.
Configure it no larger than independently verified model capacity. Before HTTP,
the OpenAI-compatible adapter checks the exact serialized request, including
system instructions, inline and response-format schemas, message wrappers and
output-token reserve. Serialized UTF-8 bytes conservatively bound input tokens;
64 additional tokens reserve service framing. This is deliberately an upper
bound, not a tokenizer measurement. Exceeding it returns
`AI_REQUEST_BUDGET_EXCEEDED` without a model call. Analyst admission includes
this full reserve for compatible adapters; process-local cumulative usage is
not a durable account spend limit or a cross-thread/retry budget reservation.
The analyst also rejects oversized raw inputs/context rather than silently
truncating them (`AI_INPUT_LIMIT_EXCEEDED`, `AI_CONTEXT_LIMIT_EXCEEDED`).
`AI_MAX_INPUT_CHARS` (1000-64000, default 6000) is the serialized
snapshot/selection context limit. Oversized snapshot contexts fail closed as
`AI_INVALID`; oversized strategy-selection contexts are rejected with an
explicit selection error. The errors include actual character count and limit.
Neither stage truncates context or treats an input-budget failure as a signal.
`CANDIDATE_RESEARCH_SETTINGS` accepts `news_max_items` (1-20, default 20); when
more in-window events are accepted, only the most recent are kept and the
snapshot records `NEWS_EVENTS_CAPPED:kept=N:omitted=M`. The US auto-loop task
uses `AI_MAX_INPUT_CHARS=16000` and `news_max_items=5` because 20 news items
pushed the October 30 US snapshots (18,276-18,612 characters) over even the
16,000-character configured cap, rejecting all ten candidates before a model
call. The task was restored to five recent news items. Offline replay of those
ten persisted contexts reduced them to 7,747-7,871 characters and passed both
AI stages' full serialized-request/schema/output-reserve admission with mocked
transport. This is request-budget validation, not a successful live-model
assessment or permission to trade. The existing producer records omitted-event
counts explicitly; this does not change universe size, provider credits or
strategy/risk rules. A running worker requires a separately authorized controlled
restart; the staged 30-instrument expansion must not be activated as a side effect.
Compose mounts `secrets/ai_api_key.txt` as the key-file secret. Keep the
non-secret endpoint, model, and session settings in the ignored host `.env`.

The API-backed Streamlit dashboard includes advisory research, ranked
opportunities, paper-order review, and explicit proposal acceptance. Before
submitting a proposal it checks that `/health` reports `PAPER`; the API
independently enforces PAPER mode, proposal freshness, and RiskEngine approval.
The legacy dashboards and their local files are not changed by this UI.

### US Twelve Data monitoring capacity

The US automatic PAPER worker shares one Twelve Data REST cache and credit
allocation across research, entries, position/pending-order monitoring and
Market Watch refreshes. India routing/readiness, strategies, risk limits,
30-second default protective cadence and execution freshness are unchanged.
There is no streaming interface/transport or confirmed WebSocket entitlement
in the existing configuration. `TWELVEDATA_MONITORING_MODE_US=websocket`
therefore reports unavailable and blocks new entries. It opens no connection
and does not claim a REST fallback establishes streaming readiness. Protection
continues best-effort REST monitoring, never simulated stream ticks.

Quotes are reused for at most `min(monitor interval, 30 seconds, execution
maximum age)`, and their original source timestamp and full quality/tick/
identity checks apply on every read. No failed read returns a stale fallback.
Per-symbol single-flight locks bound contention to 250 ms, rather than launch
duplicate requests; concurrent callers can receive an explicit timeout.
Identical OHLCV windows share a 30-second bounded cache (256 entries), with
copies preventing caller mutation. A one-second quote-failure cache prevents
immediate duplicate failed attempts. Existing caller freshness checks remain.
This does not make one-minute bar closes a continuous/execution-grade feed.

The private SQLite sidecar `TWELVEDATA_CREDIT_LEDGER_US` defaults to
`outputs/twelvedata_us_credits.db`. It is independent of the trading schema:
no trading database migration or reset is required. Each physical request is
atomically charged **before** transport, including failures/timeouts/retries.
Minute usage is a rolling 60-second window; daily counters use UTC dates and
survive restarts. Preserve/back up the ledger; never delete it to reclaim
credits. Ledger corruption, lock failure or backward clock movement is an
explicit unavailable condition, not an empty budget. Health/status reads use
read-only ledger transactions and do not call the provider or refresh prices.
All participating US workers must use the same persistent sidecar path.

Existing-position/pending-order quotes have priority. Other reads must leave
the current protected symbols' remaining-session and rolling-minute demand
plus at least 30% headroom untouched. Requests are still bounded by the overall
daily/minute caps; protection cannot invent quota once the account is exhausted.
Research and monitoring use separate resilience lanes/locks but the same
underlying allocation. Single-symbol `time_series` is the supported adapter
operation; no batch transport was introduced. Batching would still cost each
symbol's endpoint credits and would not solve the Basic-plan capacity deficit.

Status is exposed in `/health`, authenticated `GET /paper/monitoring-capacity`,
`GET /paper/loop` and the dashboard Health tab. It includes mode/connectivity,
quota provenance, local counters, forecast, blockers, monitor timing/readiness,
and last valid source time/age/error for every protected symbol. Open positions
and pending orders are read coherently under a bounded execution lock.
Unsupported streaming reports zero connections/subscriptions/reconnects,
required unique symbol slots and unknown account subscription weights/limits.
No unrestricted WebSocket access or credit savings are assumed.

Forecast notation: `N` is current distinct open/pending symbols; `M` is
`max(configured position limit, N)`; `U` is the configured universe; `C` is its
candidate limit (respecting loop top-N/max-candidates); `W` is watched symbols;
`I` is the actual protective interval in seconds; `D` is the session length
from the US calendar (including early closes); `J` is the research interval
in minutes; `c` is endpoint credits **per symbol**; `T` is the daily trade cap.
With reserve multiplier `s = 1 + reserve_pct/100`:

- Current monitoring: `N * ceil(D/I) * c`; configured monitoring substitutes `M`.
- Each cycle: `(2*U + 2*C) * c`: scanner bars/quotes, candidate bars, one
  selected deterministic strategy's bars. Failed filters can reduce actual use.
- Incidental daily scenario `A = (U + W*watch_refreshes + 2*T) * c` includes
  daily risk-stat bars and entry quote allowance. Watch refreshes are manual;
  their configured rounds are a planning scenario, not an automatic schedule.
- Daily forecast: `ceil(s * (M*ceil(D/I)*c + ceil(D/(60*J))*cycle_cost + A))`.
  Admission compares the equivalent remaining-session forecast against the
  daily limit minus the operator baseline and all local ledger charges.
- Minute forecast is the larger of the rounded sustained demand
  `s*(M*ceil(60/I)*c + cycle_cost/J + A/(D/60))` and the monitoring/entry
  burst `s*(M*ceil(60/I)*c + 2*c)`. Current-minute headroom is checked too.
  Research bursts defer/reject at the limiter; exits never wait for research.

These are conservative uncached planning scenarios, not a bound on all possible
manual work or rejected entries. Additional work/retries are charged and can
block entries, not consume reserved protective headroom. For 390 minutes,
30-second monitoring, 10-minute full-universe cycles and no watch refreshes:
10 instruments/10 daily trades/five slots forecast **7,137 credits/day**;
30 instruments/50 daily trades/five slots forecast **11,323 credits/day**.
Five-position monitoring alone is 3,900 credits/day, ten/minute before reserve.
The previously observed Basic 8/minute/800/day allowance cannot meet this.

Before any controlled activation:

1. Independently confirm the provider's US equity/ETF entitlement, endpoint
   weights, UTC quota/reset behavior and an isolated US credit allocation.
   The configured key is also used by a separate India process; neither the
   sidecar nor this gate observes that old process or other external clients.
   Deduct/reserve their usage from the US allocation first. Do not mark the
   allocation confirmed if shared-key demand is unbounded or unknown.
2. Set `TWELVEDATA_REQUESTS_PER_MINUTE` (or its existing `_US` override),
   `TWELVEDATA_DAILY_CREDITS_US`, observed `TWELVEDATA_DAILY_USED_BASELINE_US`,
   timezone-aware `TWELVEDATA_QUOTA_EVIDENCE_AT_US` from the current UTC date,
   and only then `TWELVEDATA_US_ALLOCATION_CONFIRMED=true`. These are operator
   attestations, **not automatically verified account facts**. The baseline
   is added to all local charges, even if this conservatively double-counts
   earlier US activity. Evidence expires at UTC date rollover; never lower a
   baseline, change ledger paths or reset history to force readiness.
3. Keep the durable operator entry pause during an authorized controlled
   restart. Use the existing-account task, not the staged expansion task.
   No service was restarted or externally queried for this implementation.
4. With an adequately budgeted allocation, obtain explicit fresh observations
   through the supported watch refresh or existing-position monitoring path.
   Require at least two advancing valid source timestamps (per protected
   symbol, or a fresh observed symbol when flat), two consecutive successful monitor cycles, healthy
   recovery/monitor latency and a regular US session. A stale gap resets the
   source-update evidence. A single AAPL quote never establishes capacity.
5. Review the gate and explicitly resume the operator control only when all
   blockers are resolved. It records intent, not authorization: the capacity
   gate is rechecked before cycles/recovery admission and final paper entry
   submission, along with existing strategy, freshness, sizing and RiskEngine
   checks. It does not clear kill switches or grant risk approval.

On missing/stale/invalid/regressing quotes, connection failure or insufficient
capacity, entries are blocked and existing alert/logging surfaces expose the
problem. The monitor continues its existing safe failure behavior: no fabricated
price, stale-price liquidation or assumed protected stop. `protection_assured`
remains false; polling is best effort, not a guaranteed stop-loss execution
service. Restored valid data does not clear an operator pause. Live execution
remains disabled. Protective continuity, prospective-learning evidence and
historical daily-PnL reconciliation remain separate unresolved audit items.

Validation for this change is static only, as requested: no tests added/run,
provider calls, order submissions, subscriptions, purchases, restart, commit or
push. Runtime contention, counter rollover, recovery and combined research/
monitoring behavior still need explicitly authorized deterministic tests and
controlled operational validation before relying on this new layer.

The [paper-order audit and India readiness plan](PAPER_ORDER_AUDIT.md) records
the 2026-10-15 non-order samples, source fixes and offline acceptance evidence.
Prepared migration 26 replaces broad SQLite `data_version` replay invalidation
with a durable history-only revision shared across connections. It does not
relax genuine order/fill mutation guards. It has **not** been applied to the
operator database: a future separately authorized backup, migration and
controlled restart are required. Missing revision state/triggers fail closed.
PostgreSQL replay remains unsupported.
The multi-instrument proposal endpoint also requires news intelligence. Enable
`NEWS_PROVIDER=finnhub` and place its key in `FINNHUB_API_KEY` or
`FINNHUB_API_KEY_FILE`; Compose mounts `secrets/finnhub_api_key.txt`. Finnhub
authentication is sent in the `X-Finnhub-Token` header. Company-news events are
bounded to the requested `as_of` window and stale/future events are excluded.
US market symbols use identity mapping; for other markets set
`FINNHUB_SYMBOL_MAP` as JSON keyed by `MARKET:SYMBOL`, for example
`{"IN:RELIANCE":"RELIANCE.NS"}`. Mapping is operator-supplied, never guessed.
See [Finnhub's company-news API documentation](https://finnhub.io/docs/api/company-news)
for source endpoint and usage details.

The authenticated `POST /intelligence/opportunities` endpoint accepts 1-10
unique `instrument_ids` and a required timezone-aware `as_of`. It evaluates
each instrument through the existing research pipeline, including regime,
Finnhub news evidence and AI strategy selection, then ranks only actionable
deterministic aggregate proposals. Ranking uses aggregate confidence, then
absolute aggregate score, then instrument ID—not model-reported strategy
confidence. Each proposal includes timestamps, signal levels, regime,
strategy-selection context, evidence provenance and explanations. Proposals
are advisory: the response explicitly says `risk_status=NOT_EVALUATED` and
`execution=NOT_SUBMITTED`. They contain no order quantity and cannot execute;
any later paper submission must still pass the existing TradingService and
RiskEngine path. To explicitly submit one, call the authenticated
`POST /intelligence/proposals/{proposal_id}/submit` with a declared `operator`.
The default `sizing_mode` is `AUTOMATIC_SIZING`, which uses the existing
risk-based sizing helper. To use a supervised manual quantity, explicitly set
`sizing_mode` to `MANUAL_OVERRIDE` and provide a positive integer `quantity`.
The endpoint rejects proposals older than the configured maximum market-data
age, persists the proposal payload and a one-shot submission claim, and routes
the deterministic pipeline context through `TradingService`; `RiskEngine`
still makes the final order decision. Replays return the existing recovered
order, while an ambiguous `SUBMITTING` record blocks retry pending
reconciliation. Unaccepted proposal contexts remain process-local, are bounded
to the most recent 1,000 proposals, and are lost on restart; generate a new
proposal after restart. The declared operator is audit metadata, not a separate
user identity or authorization mechanism.

Example request body:
`{"instrument_ids":["XNYS:AAPL","XNYS:MSFT"],"as_of":"2026-10-07T15:00:00Z"}`.
An automatic acceptance body is `{"operator":"reviewer"}`. A supervised
manual override body is
`{"operator":"reviewer","sizing_mode":"MANUAL_OVERRIDE","quantity":10}`.
The service lock and active-order reservations are process-local; run one API
worker. Multi-worker operation is unsupported until cross-process reservation
coordination exists.
Optionally set `FUNDAMENTAL_PROVIDER=yahoo` to add recent Yahoo Finance
reported-EPS event evidence to research. That adapter uses past earnings-event
dates, omits undated profile data, and is research-only; stale/missing reports
produce no score. It does not provide sector direction or sector mappings.
For optional sector context, set `SECTOR_PROVIDER=nse` and
`NSE_SECTOR_INDEX_MAP` to a JSON object mapping exact instrument symbols to
NSE index names, for example
`{"RELIANCE":"NIFTY OIL & GAS","INFY":"NIFTY IT"}`. Only operator-reviewed
assignments are used; the application does not infer or refresh index
membership. The adapter is restricted to instruments identified as India/NSE
(`market=IN`, `exchange=XNSE`) and reads the NSE public live-indices endpoint.
NSE may block or rate-limit automated requests; failures make research
unavailable rather than substituting guessed data. The endpoint is not a
guaranteed data feed, so verify access and usage terms before operational use.

For legacy AI research outside personal mode, set `RESEARCH_SESSIONS` to a JSON object keyed by
configured market code. Each market entry must explicitly supply
`opening_range_minutes`, `entry_cutoff`, `square_off`, and `late_entry_start`
as minutes or local `HH:MM` values respectively. `entry_start` is optional;
market timezone/open/close are taken from the selected market definition.
Example for explicitly configured US hours:
`{"US":{"opening_range_minutes":15,"entry_cutoff":"15:00","square_off":"15:55","late_entry_start":"12:00"}}`.
Do not copy these sample values without reviewing the session policy for the
target instruments. Requests whose market-local date is outside the supplied
exchange-calendar coverage fail closed with 503. AI output remains advisory
and the endpoint does not submit orders.

## Manual restart-safe PAPER cycle (Phase 2C-6/2C-7)

The authenticated PAPER-cycle API and CLI compose configured research,
deterministic signal generation, persisted risk approval, paper-order
submission, and position-exit management. Use them only after applying
migrations and confirming the research provider, market sessions/calendar,
risk settings, database, and PAPER mode are ready. The cycle requires
configured AI research; it is a bounded manual operation, not a scheduler or
unattended trading service. Keep one API worker because portfolio/order
reservations and portions of runtime state remain process-local.

The API request requires `universe_id`, `idempotency_key`, and `operator`;
`as_of` is optional but must be timezone-aware when supplied, and `top_n` is
optional and bounded by the service's configured candidate cap (API maximum
10). The cycle is PAPER-only; the strict request schema does not accept a mode
override.

```json
{
  "universe_id": "us-equities",
  "idempotency_key": "manual-cycle-20261008-01",
  "operator": "reviewer",
  "top_n": 5
}
```

Authenticated endpoints:

- `POST /paper/cycles` runs one bounded cycle. Reusing an idempotency key with
  a different request is rejected; replay of a completed request returns its
  persisted result.
- `GET /paper/cycles?unfinished_only=true` lists unfinished runs;
  `GET /paper/cycles/{run_id}` returns its run, candidate checkpoints, and
  events.
- `POST /paper/cycles/{run_id}/recover` explicitly resumes an interrupted or
  partial run after recovery checks and lease acquisition.
- `POST /paper/positions/manage` explicitly evaluates current positions for
  protective exits; no background polling is installed.
- `POST /recovery/reconcile` reconciles persisted PAPER orders, executor state,
  and fills. Review discrepancies and gate state before using the explicit
  `/recovery/resume` workflow.

CLI equivalents (run with the configured environment/database):

```powershell
python -m stockmarket paper-cycle --universe us-equities --idempotency-key manual-cycle-20261008-01 --operator reviewer --top 5
python -m stockmarket paper-cycle-show --run-id <run-id>
python -m stockmarket paper-cycle-recover --run-id <run-id>
python -m stockmarket paper-recovery
python -m stockmarket paper-positions
python -m stockmarket paper-positions-manage
python -m stockmarket paper-orders
```

Paper executor orders, execution attempts, and fill sequence are persisted
(migrations V13/V14). Restart recovery reconciles these records with order and
fill ledgers; an inconsistent or ambiguous state blocks new entries. A retry
is allowed only when durable executor state proves that the stable client order
was never accepted. Do not manually edit cycle, proposal, order, or fill rows
to force a retry. A restored resting paper order needs a fresh quote update
before it can fill. An expiring account-scope lease prevents concurrent cycle
runs; expired runs still require explicit recovery rather than automatic
takeover.

## Opt-in PAPER auto loop (local algorithm verification)

The Trading Monitor shows loop state above the tabs. Use the sidebar's
"Paper cycle to inspect" selector to review up to ten recent persisted cycles:
Research shows run history/context; Opportunities shows deterministic intent,
strategy reasons, risk stage and order status; Strategies shows the run's
configured catalog and advisory selections; News & AI shows persisted
assessments and snapshot evidence. These views use authenticated read-only API
routes and do not start research or submit orders. COMPLETE means processing
finished, not that an entry was generated or filled.

Standalone news research now saves validated news events and successful news
analyses through the existing news repository. Old events are not backfilled;
automatic-cycle snapshot evidence remains a separate persisted source and is
displayed separately. A failed AI analysis never becomes a successful record.

Health exposes `last_research_data_timestamp` (the newest bar in the most recent
validated historical response) and `last_research_data_received_at` (when that
read completed). Neither is an execution-feed freshness approval. Only validated
quotes/freshness-required bars update `last_market_data_timestamp`; missing or
stale execution data continues to report DEGRADED. Activity timestamps are
process-local and reset at restart; cycle/research history remains in the database.

`PAPER_AUTO_LOOP=true` builds a background loop inside the single API process
that runs the existing restart-safe PAPER cycle on a fixed cadence during the
regular session of one market. It never runs in LIVE mode, with a live
executor, or with `PERSONAL_RESEARCH=true`; unsafe or unparseable settings are
startup `ConfigurationError`s. Defaults are off.

| Variable | Default | Meaning |
|---|---|---|
| `PAPER_AUTO_LOOP` | `false` | `true`/`false` only |
| `PAPER_AUTO_LOOP_MARKET` | first of `MARKETS` | must be an enabled market |
| `PAPER_AUTO_LOOP_UNIVERSE` | `<MARKET>_ALL` | scanner universe |
| `PAPER_AUTO_LOOP_INTERVAL_MINUTES` | `15` | cadence, aligned to wall-clock slots |
| `PAPER_AUTO_LOOP_TOP_N` | unset | all eligible instruments; optionally set a positive research subset |
| `PAPER_EXPLORATORY_AUTO_ENTRIES` | `false` | `true` lets unvalidated strategies open PAPER positions automatically to observe P&L; all strategy, sizing, freshness, session, risk and exit gates still apply; cycles/decisions are labelled `EXPLORATORY_UNVALIDATED` and never count as validation (goal rule 5.1 exception) |
| `AUTOMATIC_EVIDENCE_MAX_AGE_DAYS` | unset | 1-365, operator-chosen. Automatic selection may use only an `ACTIVE_FOR_PAPER` Strategy Lab version for that instrument that still passes `paper-review-v1` and whose holdout ended within this window. Unset keeps every automatic entry blocked (`NO_VALIDATED_STRATEGY`) |
| `PAPER_DISCOVERY_ENABLED` | `false` | adaptive discovery: rank long-HOLD instruments after fresh scanner candidates; never authorizes an order |
| `PAPER_DISCOVERY_HOLD_MINUTES` | `30` | 5-1440; continuous HOLD/NO_TRADE time before an instrument is rotated |
| `PAPER_DISCOVERY_COOLDOWN_MINUTES` | `60` | 5-1440; rotation lifetime before the instrument competes normally again |
| `PAPER_DISCOVERY_MAX_ROTATED` | `20` | 1-500 rotated instruments (watchlist capacity) |
| `PAPER_DISCOVERY_HISTORY_CYCLES` | `20` | 2-200 persisted cycles read to compute HOLD streaks |
| `PAPER_MONITOR_INTERVAL_SECONDS` | `30` | 1-300 seconds; pending orders and protective exits between scans |
| `PAPER_MONITOR_LATENCY_BUDGET_SECONDS` | `10` | positive finite soft cycle budget, at most 300 seconds |
| `PAPER_MONITOR_LOCK_TIMEOUT_SECONDS` | `1` | execution-lock admission timeout, no greater than budget |
| `PAPER_MONITOR_QUOTE_TIMEOUT_SECONDS` | `2` | quote future wait, no greater than budget; one attempt |
| `PAPER_MONITOR_MAX_ITEMS` | `100` | 1-10000 order/position checks per pass; incomplete passes block entries |
| `PAPER_EOD_FLATTEN_MINUTES` | `10` | minutes before close to stop entries and flatten |
| `MARKET_STATS_LOOKBACK_DAYS` | `20` | completed daily bars for ADV/slippage/correlation |

Each tick: outside the session it does nothing; in the last
`PAPER_EOD_FLATTEN_MINUTES` it closes every open paper position for the market
(`END_OF_DAY_FLATTEN`, `SELL_TO_CLOSE`/`COVER` through RiskEngine and
OrderManager); while the kill switch is active it only manages exits; otherwise
it runs one cycle with the deterministic idempotency key
`loop-<UNIVERSE>-<UTC slot>` so a restart within a slot cannot duplicate
entries. The slot identifies the request, not the age of its evidence: new
cycles use the service's current clock, and retries retain the persisted
research timestamp. This avoids stale research after host sleep without
relaxing freshness checks. Signal generation, like personal research, excludes
the still-forming intraday bar after validating the full provider response.
Unfinished cycles are recovered at startup unless automatic entries have been
explicitly paused. PARTIAL/FAILED cycles and startup recovery failures count
towards the failure limit. `STOPPED_AFTER_FAILURES` disables new scans, not
protective monitoring: ticks remain `EXITS_ONLY`. Resolve failures before restarting
the API; operator resume cannot override failure exhaustion. A host shutdown stops
the worker before closing its provider; a timed-out join is an explicit failure,
not a reported successful stop. `GET /paper/loop` (bearer token) reports state,
monitoring cadence, automatic-entry control, collection results, failure counts
and per-cycle startup recovery failures.

Repeated ticks within a scan slot manage positions without repeating research.
The 30-second monitoring interval allows a 30-minute scan schedule to reach the
10-minute close window, including known early closes. Pending entries/exits are
refreshed with validated quotes and reconciled through the existing OrderManager;
pending entries are cancelled before close-window flattening. One unavailable
position quote yields an audited `MONITOR_FAILED` without preventing other
positions from being checked. Any such failure blocks new entries. Real broker
liquidity is not simulated: partial-fill tests use bounded test liquidity.

The loop automatically reconciles prospective attachments after successful
monitoring/recovery/cycles. Integrity/collection failures block new scans while
exits remain enabled; immutable decisions and dataset boundaries do not change.
All monitoring GETs remain read-only. Strategies exposes readiness, missing features,
frozen decisions and their subsequent events. No model training is performed.

Use the System health automatic-entry form, or authenticated
`POST /paper/loop/control` with
`{"entries_enabled": false, "operator": "reviewer", "reason": "maintenance"}`.
Repeat requests for the same desired state are idempotent. Controls serialize
after the active tick; pause does not undo already submitted orders. State is
restored from the existing audit log at startup, without a new migration. Resume
requires `true` and does not clear kill switches or grant risk authorization.
Pending interrupted cycles are deferred while paused; restart with entries
enabled to apply startup recovery before resuming ordinary scans.

`GET /health` includes `paper_readiness`: closed/unsupported sessions, missing/stale
execution data, unhealthy services, halted gates and disabled automatic entries
are explicit blockers. `PER_ORDER_VALIDATION_REQUIRED` is not approval: every order
still passes independent data, signal, sizing and risk checks. Closed-market stale
quotes are not refreshed or fabricated to make health green.

The opt-in loop now starts a separate `paper-protective-monitor` worker and a
deadline observer. The research loop does not hold the supervisor's scheduling
lock, and scan-boundary position checks use its recent status instead of performing
duplicate polling. A single authoritative TradingService account reservation
still serializes order/position mutations and prevents duplicate protective exits.
Manual direct ticks before background startup retain the existing synchronous path.

The supervisor has one active cycle, never a replacement worker launched for a
stall. It reconciles pending orders, checks positions and cancels entries/flattens
in the close window through the existing lifecycle/RiskEngine/OrderManager.
Order/position item counts and monotonic deadline checks bound admission to more
work; incomplete item-limited passes rotate the work list rather than starving
later positions, and continue to block entries. They do not cancel an atomic
update already in progress. The independent
quote lane cannot be queued behind research futures, uses a configurable wait
timeout with one attempt and no rate-limit sleep, and preserves all quote-quality
and freshness checks. Protective exits do not fetch entry-only liquidity/
correlation statistics; RiskEngine's applicable exit checks are unchanged.

At application composition, existing RecoveryManager restores/reconciles durable
orders, fills and positions before workers start. No entry is admitted until startup
cycle recovery completes and a fresh successful supervisor pass exists. A failed
startup cycle remains explicitly unresolved and blocks entries even if later
protective checks succeed; resolve/review its persisted history before restarting.
The supervisor never retries interrupted research orders on its own. Existing
pause/resume audit persistence and kill-switch rules remain authoritative.

Read `GET /paper/loop` with the bearer token (also displayed under System health).
Its `protective_monitor` object includes last start/end/success, duration, recent
and maximum durations, success age, deadline misses, consecutive failures, items
checked, recovery completion, last error and `entry_ready`. GET does not submit
orders or perform reconciliation. `STALLED`, `STALE`, `FAILED` or incomplete
recovery blocks new entries, including the final TradingService order gate.
An exception/failure never updates last-success time. The worker retries monitoring
on the next interval; a clean pass can restore readiness after transient failures,
but cannot clear failed startup recovery or a kill switch. Deadline detection
reports a stall without interrupting or fabricating the execution result.

Shutdown first prevents new loop work, signals both monitoring workers, and joins
within the shared shutdown timeout. If order work is still active, shutdown raises
and the API does not close its provider underneath it. Python cannot safely kill
an active provider, broker or database call; timeout cancellation applies to
queued quote futures, not necessarily already-running vendor requests. Outstanding
vendor reads may still survive a provider close and process termination may wait
for those executor threads.

This remains single-process PAPER automation, not a hard-real-time or multi-worker
scheduler. The execution RLock/account reservation and SQLite connection lock are
shared with entries and persistence. An entry already fetching historical risk
statistics inside the execution boundary was the baseline bottleneck. Entry
statistics/quotes now prepare outside it, with final freshness/state/risk checks.
Nested caller-owned critical sections and atomic database writes can still delay
monitoring; the watchdog blocks entries but cannot force a timely exit.
Native calls/GIL pauses, saturated quote workers and vendor-internal locks/rate
limits are additional limits. Timed-out reads never mutate orders. Bounded
history/provenance and prospective replay still have scaling ceilings; item limits
can block entry readiness when the configured universe is too large for a pass.
Operational telemetry is process-local, with start/end/failure logs; it is not
strategy profitability evidence or a model-training dataset. Calendar coverage is still limited
to registered supported years/markets, and unavailable calendars fail closed.
Restart the operator-managed API with the updated code/configuration after
backing up its database; this development stage does not restart running services.
An old API/base URL can still return 404 for `/paper/fills`; the dashboard now
reports unavailable history rather than misrepresenting that error as zero fills.

### Execution preparation and bounded storage

Entry preparation takes a brief execution-lock snapshot, releases the lock,
fetches scalar statistics and required quotes, and freezes them with start/end
timestamps, source identity and quote completeness. Final execution rejects
expired preparation or changed cash, positions, FX, fills or orders, rechecks
entry admission/kill/strategy controls and runs RiskEngine on current mutable
state. Open-order reservation quotes use that same prepared snapshot. Automatic
sizing still runs under the authoritative reservation using current exposure and
cash; preparation is not an approved risk decision. Preparation inputs are also
recorded in order-audit risk inputs. A changed snapshot is rejected, not retried
automatically. A monotonic, process-local portfolio revision also detects transient
changes restored to identical values; rollback never rewinds that revision.
It is not a durable approval or a cross-process synchronization mechanism.
Custom callers that hold an outer execution lock before calling
submission cannot benefit from this isolation; do not perform preparation inside
caller-owned critical sections.

Each provider lane admits at most four outstanding futures, with no unbounded
queue/replacement workers. A timed-out running read keeps its slot until it
really completes; late results are discarded by the timed-out caller, never
sent to order mutation. Close rejects new reads and cancels queued work without
waiting for uninterruptible vendor reads. The underlying vendor object is still
shared; its locks/rate limits, native calls and the GIL are not isolated processes.

SQLite defaults to a 1-second Python-lock admission timeout, a 1-second native
busy timeout and a 1-second statement progress deadline (constructor keyword
overrides support `(0, 300]` seconds). These are separate budgets, not one
end-to-end deadline. The progress callback runs every 1,000 SQLite VM operations;
it cannot interrupt every I/O/native call. Re-entrant transaction ownership is
preserved and failures roll back; commit/rollback/fsync durations are not hard
bounded. Timeout raises explicitly. An interrupted execution retains a durable
recovery requirement; never clear that claim without reconciliation. PostgreSQL
still needs separately validated lock/statement settings and pool isolation.

Routine execution state verification reads only bounded identity/status/economic-field
projections (10,000 fills/orders), not full payloads. Position provenance queries
only that instrument's current episode (1,000 fills). Orders/fills replay via
keyset pages of 500 rows, capped at 100,000; orders use timestamp plus identity,
while fills reuse the durable feedback ordinal to preserve same-timestamp
execution ordering. Unique-time legacy fills replay first, as in outcome feedback;
ambiguous same-time legacy fills require reconciliation rather than guessed order.
Interruption
raises, never returns a completed partial portfolio. Replay also validates page
advance, expected row counts, contiguous known ordinals and SQLite local/external
change revisions. Recovery restarts replay
from durable data rather than trusting a partial in-memory checkpoint. It assumes
the existing single-process startup admission barrier; this is not snapshot
recovery that can admit entries while arbitrary external writers are active.
Changes observed during replay abort it; final execution-reserved projections
also reject changed order/fill economics. No mechanism repairs externally edited
financial records automatically.

Prospective decisions/attachments/outcomes cap at 10,000, per-order fills at
1,000 and events at 10,000. Overflow refuses reconciliation/dataset creation;
it does not silently truncate. Replay commits one decision at a time with a
1-second boundary-checked processing budget and bounded source reads. Earlier
completed decisions survive interruption; the current decision rolls back and
retry is idempotent. Only a fully returned replay reports `complete=true`.
Point-in-time features, label checks, membership and holdout isolation are unchanged.
Larger histories need checkpointed snapshot/replay design, not raised limits
without validation. Operational telemetry remains unsuitable for model training.

Migration **V24** adds indexed `(timestamp, identity)` order/fill replay and
`(instrument_id, timestamp, fill_id)` provenance access. SQLite query-plan tests
verify indexed replay seeks without temporary sorts. Position filtering uses its
instrument/time index but may need a secondary ordinal sort for tied timestamps.
Only disposable test databases
were migrated during this milestone. No operator database was changed. Back up
and review pending migrations before an operator-managed upgrade; do not start
the new application against an operator database implicitly to test it.

Offline stress harness (no credentials/network; creates only test state):

```powershell
$env:PYTHONPATH = 'src;tests'
python -m unittest execution_isolation_stress
```

### Protective-exit preparation and recovery validation

Both ordinary monitoring and session flattening now prepare selected quotes outside
the execution lock/account claim. They briefly snapshot current positions (including
opening identity, side/quantity and portfolio revision) and pending orders, release
the boundary for provider calls, then acquire a new reservation and compare current
state. A changed snapshot reports `MONITORING_STATE_CHANGED` and performs no prepared
price update/exit. The next complete monitoring pass may recover; the rejected pass
does not retry an old quote or imply an exit succeeded.

Each successful quote freezes instrument identity, adapter-callable source,
provider timestamp and fetch timestamp. Map membership records availability;
exceptions/missing data remain explicit per-instrument failures. Current instrument,
tick grid, session, position opening time and freshness checks remain authoritative.
Nested exit risk/reservation calculations use that map, never a missing-key provider
fallback. Final RiskEngine still evaluates current quantities, exposure and controls.
Prepared quote metadata/completeness is persisted with the ordinary order audit.

A process-local lifecycle single-flight lock prevents overlapping preparation by
direct callers as well as the supervisor. Entry admission reports
`PROTECTIVE_RECONCILIATION_IN_PROGRESS` during provider preparation and
`PROTECTIVE_RECONCILIATION_INCOMPLETE` following failure, stale input, partial work
or changed state. Only a complete successful lifecycle check clears this barrier;
it never clears another owner's kill/pause/recovery reason. A concurrent entry
rejection itself persists a new order and can invalidate the prepared snapshot;
that conservative failure is intentional. Cancellation remains available and also
invalidates affected preparations. The existing startup barrier replaces this
process-local state after restart. Quote work is capped by the monitoring item
limit as well as the deadline; exhausted budgets are incomplete, not healthy.

Provider waits in the lifecycle and its nested exit submission are removed from
the atomic pass. Durable provenance reads, current-state validation, database writes
and broker price/fill updates remain inside it. Entry preparation by a custom caller
already holding an outer lock, vendor internals, native/GIL calls and shared database
admission can still delay protection. A stalled provider read cannot be cancelled
by a Python timeout and continues consuming its bounded lane slot. The watchdog
does not promise a timely exit.

Recovery starts with its own entry gate closed. Orders/fills are counted before
replay; an explicit process-local completion flag also prevents `resume` or
reconciliation from declaring success after interrupted order replay. An operator
acknowledgement cannot substitute for completed replay. Histories exceeding
100,000 replay records or 10,000 execution-verification records are refused.
Tests use a real disposable 100,001-fill history and verify that
neither a partial portfolio nor a new entry is published. Missing pages, non-advancing
keys, ordinal gaps, row-count mismatches and SQLite revision changes abort replay.
The SQLite revision combines `PRAGMA data_version` (other connections) and
`total_changes` (this connection); unrelated writes can conservatively invalidate
replay too. A fresh attempt restarts from durable data. Restoring a conflicting
already-loaded order fails and requires a fresh restart/reconciliation, rather than
silently retaining old state. Successful replay is not an immutable multi-query
snapshot for arbitrary external writers, and cannot discover a legacy deletion
that predates all available audit/order evidence.

No durable checkpoint or migration is introduced. Existing PnL snapshots lack a
complete accounting/FX/risk-baseline state plus an atomic replay cursor, and the
execution-verification ceiling would still refuse large histories. A future
checkpoint needs versioned complete accounting state, a fill high-watermark and
integrity digest committed in the same fill transaction, validated delta replay,
legacy coverage, atomic publication and durable current-state verification. Do not
raise ceilings, discard old records or manually clear recovery gates as a shortcut.

PostgreSQL assessment: `PostgresDatabase` exists, uses one connection and one RLock,
and has no pool, admission deadline or default statement/lock-timeout configuration.
The new validated history-revision contract is deliberately unsupported there;
guarded startup replay fails explicitly rather than claiming SQLite-equivalent
snapshot safety. PostgreSQL-backed paper startup is therefore not certified.
No PostgreSQL server/driver/test DSN was available for this validation.

Opt-in integration tests in `tests/test_postgres_isolation_integration.py` cover
single-connection saturation, explicitly configured session timeouts, transaction
contention/rollback and explicit refusal of unsupported validated replay. They skip
without `STOCKMARKET_TEST_POSTGRES_DSN`; if supplied it must be a URI pointing to an
explicitly disposable `stockmarket_test_*` database. They create only session-local
temporary data, use an advisory transaction lock, and do not apply migrations.
Install the optional psycopg driver in a separate approved test environment before
running them. Skips are not successful PostgreSQL validation. Required follow-up:
bounded pool/admission, backend-owned timeout settings, consistent snapshot/revision
semantics, complete restart recovery and atomic financial integration tests, actually
executed against supported PostgreSQL versions.

Use the configured Python interpreter. The harness reports sample counts,
p50/p95/worst observed scheduling/lock/quote/database timings, rejected entries,
deadline misses and duplicate-fill assertions. It uses Events to hold history,
a controlled database owner and 20 scheduled cycles. Measurements are finite
fixture observations, not a guaranteed maximum, profitability claim or SLA.
Telemetry buffers are process-local recent tails; baseline scheduling/database
telemetry was unavailable and is explicitly reported with zero samples.

When recovery finds a research idempotency conflict caused by configuration
changes, it terminates the old cycle as `FAILED / RECOVERY_OBSOLETE` without
retrying entries. The original request, failure, candidate records and events
are retained; a timestamped recovery disposition and new events explain the
termination. It releases the old cycle scope so a new slot can use current
configuration. This is a resolved obsolete cycle, not a successful trade.

Migration V17 adds a durable account submission reservation. A competing
submitter cannot acquire it, including after expiry, without reconciliation.
Submission checks persisted order/fill identities against its in-memory state;
stale workers fail closed rather than spend stale cash. Interrupted claims
require reconciliation. Startup recovery removes failed or expired claims only
after a clean order/position reconciliation; an unexpired ACTIVE claim blocks
recovery. Never run a second worker as a way to clear a claim. Cycle lease
renewal/ownership checks use the injected decision clock and do not revive an
expired lease.

PaperBroker independently rejects stale/future/naive quotes and will not match
a resting order against an expired cached quote. Durable resting fills and
executor state are written transactionally; a persistence failure restores the
in-memory portfolio and executor record instead of leaving a phantom fill.
The Orders tab separates accepted orders from actual simulated fills using
the authenticated read-only `GET /paper/fills` route. Health displays loop and
startup-recovery errors. These monitoring reads do not invoke AI or execution.
Realized PnL is gross price PnL; fees are recorded separately and deducted from
cash. For a fully closed fixture, net PnL is gross realized PnL minus fees.

### Completed paper outcomes and AI feedback

Migrations V18/V19 add immutable closed-position outcomes, fill-ingestion ordering,
trusted execution timeframe attribution, and versioned strategy-laboratory records.
Back up the configured database and apply migrations using
`python -m stockmarket.ops migrate` during a controlled maintenance window.
These changes do not migrate legacy JSON/CSV artifacts. Do not run competing
workers or change execution mode to install them.

The paper fill transaction also projects completed position episodes and appends
`PAPER_OUTCOME_RECORDED` to the audit chain. Partial closes accrue inside the
episode; only returning to flat emits an outcome. Additions, short covers and
reversals share the portfolio's weighted-average accounting, with proportional
fee allocation for a reversal. Actual fill prices already incorporate slippage:
reported slippage is diagnostic and is **not** deducted from PnL again.
Restart replays persisted fills idempotently, backfilling missing outcomes without
rewriting trades. A fill/PnL conflict or ambiguous same-time legacy fill ordering
is a visible recovery error, not silently replaced history.

Execution attribution comes from the actual registered paper strategy's version,
configuration hash and interval, not AI confidence or client-supplied audit
metadata. Missing historical attribution stays incomplete; mixed-version
episodes do not enter strategy-ranking feedback.

Authenticated monitoring routes are read-only:

- `GET /paper/outcomes?limit=100`: completed episodes, fill identities, reasons,
  attribution, gross PnL, fees, net PnL and holding time.
- `GET /paper/strategy-performance`: metrics grouped by market, currency,
  strategy/version/configuration, timeframe and entry regime.
- `GET /strategy-lab/proposals?limit=20`: proposals and persisted evaluations.

The Strategies tab displays these separately from legacy/backtest trade metrics.
Monitoring never processes outcomes, invokes AI or submits orders.
Metrics include expectancy, win rate, profit factor (undefined without losses),
closed-PnL drawdown, nonannualized return volatility and mean-to-volatility ratio.
The approximate expectancy confidence half-width is unavailable for one trade.
Thirty completed, attributed episodes qualify only as a **paper sample**, not
out-of-sample proof, profitability or permission to change risk.

Both AI candidate assessment and strategy selection receive a bounded
`outcome_feedback` context, persisted with the assessment and its prompt hashes.
Only outcomes both closed and known by the research `as_of` are available.
Backfilled outcomes cannot affect earlier decisions. Evidence must match the
registered strategy version/configuration and market/currency; no FX comparability
is inferred. At most four performance groups are supplied, with omitted counts.
Historical feedback is advisory; all deterministic execution gates remain intact.

### Constrained AI strategy laboratory

Explicit authenticated mutations (not monitoring) are:

- `POST /strategy-lab/proposals` with `{"snapshot_id": "<persisted-snapshot-id>"}`.
- `POST /strategy-lab/proposals/<proposal-id>/evaluate` with timezone-aware
  `start`/`end`, optional `commission_rate` (default `0.001`) and
  `slippage_bps` (default `5`).

Proposals reuse validated research evidence and the configured AI provider.
Currently only bounded ORB/VWAP parameter modifications at 5m/15m are representable.
Entry/exit/data/sizing policy identifiers are fixed; extra fields and unsupported
templates are rejected. Hypotheses, alternatives and invalidation descriptions
are inert research text, never Python, SQL or shell execution. NO_TRADE is stored
and cannot be evaluated for execution.

Evaluation uses the existing causal backtester and frozen parameters, with
20/10/10-session train/validation/test windows, one-session embargoes, next-bar
execution, positive costs and participation assumptions. Reports retain bars
hashes, costs, sizing inputs, skips and segment metrics. Insufficient history
produces an explicit `INSUFFICIENT_EVIDENCE`; provider errors remain errors.
Requests are bounded to past data spanning at most 180 days.

**No laboratory proposal is executable or automatically registered.** Historical
reports remain exploratory; retrospectively proposed parameters do not become
out-of-sample evidence merely by splitting historical dates. Even large historical
samples remain `EXPLORATORY_NOT_ELIGIBLE`. Existing registered strategies still
use the unchanged signal -> risk -> order -> paper broker path. Historical FX,
new exchange calendar verification and multi-worker certification remain gaps.

### Session flattening and preregistered post-proposal holdouts

Migration V20 adds one immutable holdout registration per proposal. Registration
is explicit, authenticated, and does not invoke AI, retrieve market data or
submit orders:

`POST /strategy-lab/proposals/<proposal-id>/holdout`

```json
{
  "start": "2026-11-02T09:30:00-05:00",
  "end": "2026-12-01T16:00:00-05:00",
  "commission_rate": 0.001,
  "slippage_bps": 5
}
```

Dates above are illustrative: start must still be in the future when registering.
Start/end must be actual exchange session open/close boundaries, with calendar
coverage and at least one complete trading-session embargo after registration.
NO_TRADE, past windows, previous evaluated-data overlap and changed registrations
are rejected. Identical registration retries return the original record.
The window, proposal/configuration identity, instrument metadata, session/calendar,
sizing, transaction costs and flatten policy are frozen together.

After the reserved window has finished, call the existing evaluation route with
the same dates/costs and `"evaluation_kind": "POST_PROPOSAL_HOLDOUT"`.
An unregistered window or changed policy fails closed. Historical exploration
cannot request reserved holdout dates. Missing whole sessions or scheduled bars
are rejected; truncated data cannot manufacture an end-of-day exit. A persisted
holdout report is returned unchanged on retry or service reconstruction without
retrieving data again, including insufficient-sample reports. There is no
window extension/replacement or parameter fitting on observed holdout results.

Laboratory evaluations now use the backtester's opt-in session-flatten policy:
exchange close minus the configured paper-loop flatten margin (10 minutes when
the loop is disabled), aligned **at or before** the cutoff on the scheduled bar
grid. Exchange timezone, DST and early closes determine the boundary. Pending
entries are cancelled before that bar can fill; no new signals are generated
after it. Long SELL exits and short BUY covers use boundary bar **open**, with
costs/slippage and participation limits. Partial flatten fills continue on later
session bars; insufficient liquidity to become flat by the last session bar is
an explicit evaluation failure. This is a bar-level simulation, not proof of
intrabar quote/execution parity. Missing scheduled flatten bars are errors.
Existing backtests keep their original behavior unless flatten is enabled.

Reports distinguish historical folds from preregistered future observation.
At least 20 full holdout sessions and 30 completed trades produce
`HOLDOUT_REVIEW_REQUIRED`; otherwise the status is `INSUFFICIENT_EVIDENCE`.
Neither status makes a proposal executable, establishes profitability, changes
risk, or proves absence of multi-proposal selection bias. The Strategies tab
shows persisted registrations, reports and policies through GET-only monitoring.
An authorized lifecycle transition now supports explicit paper approval/activation.
Independent prospective comparison and regime-specific validation remain required;
positive backtests never auto-promote a proposal.

### Evidence-based strategy intelligence

The existing authenticated lifecycle mutation is
`POST /strategy-lab/proposals/{proposal_id}/transition`, with `target`, `operator`,
`reason`, unique `request_id` and `acknowledge_selection_bias` for approval.
Legal progression is DRAFT -> EVALUATED -> HOLDOUT_REVIEW_REQUIRED ->
APPROVED_FOR_PAPER -> ACTIVE_FOR_PAPER. Evaluations update evidence states;
operators explicitly approve and activate. ACTIVE can PAUSE or RETIRE; PAUSED
can reactivate after current eligibility checks; RETIRED is terminal. Pending
entry orders block deactivation. Existing positions retain normal risk-checked
exits. Active frozen versions use the existing trusted signal, RiskEngine,
OrderManager and PaperBroker path; neither this lifecycle nor the comparison
service enables live execution.

Ranking policy `advisory-evidence-v2` reads persisted outcomes directly, not
AI-provided feedback statistics. Each compared strategy must match the instrument,
market, currency, version, parameter hash, timeframe and recorded entry regime.
The current decision regime is explicitly UNKNOWN, not a fabricated categorical
regime. CLOSED and KNOWN timestamps must both precede/equal the decision timestamp.
Samples qualify at 30 trades on 10 observation dates, split into three chronological
blocks. Score is net-return expectancy minus catalog-Bonferroni normal uncertainty,
block-mean standard deviation, negative worst-block expectancy and closed-return
drawdown divided by trade count. Profit factor, win rate, fees, net/gross PnL,
observation IDs and configuration identity are recorded separately.

This is advisory, not a calibrated predictive probability. Overlapping episodes
are explicitly rejected by this estimator. Only qualified alternatives reorder
qualified slots; insufficient evidence does not receive score zero or outrank a
known loss. Only AI-recommended alternatives participate in this reordering; the
record discloses compared names and full catalog size. If every compared alternative
has a qualified nonpositive score, assessment becomes NO_TRADE before signal/order
creation. Existing insufficient-sample research behavior otherwise remains intact.
Past persisted ranking records retain their original policy and are not rewritten.

Final POST_PROPOSAL_HOLDOUT metrics are visible for human review but never included
in research/selection feedback. Historical feedback includes validation summaries,
not final-holdout or TEST metrics. Historical exploration/comparison cannot consume
any proposal's reserved final window for the same instrument.

To explicitly run a retrospective comparison, submit an authenticated
`POST /strategy-lab/comparisons` JSON body:

```json
{
  "proposal_ids": ["<immutable-64-character-proposal-hash>"],
  "start": "2026-10-01T09:30:00-04:00",
  "end": "2026-12-18T16:00:00-05:00"
}
```

Use actual complete covered exchange sessions; dates above are illustrative.
One to eight frozen PROPOSAL versions must share instrument, market and timeframe.
The historical span is at most 180 days. Missing sessions/bars, changed template
identities, invalid inputs, future windows and reserved final holdouts fail explicitly.
The service uses the same deterministic adapters, application sizing and backtester
as the laboratory: commission 0.1%, slippage 5 bps, spread 2 bps, participation 1%,
one-bar latency and configured session flattening. Spread/slippage are embedded
in fills, not subtracted a second time.

Every fold reserves 20 TRAIN sessions, ranks using 10 VALIDATE sessions, then tests
on 10 sessions; one session embargo separates segments. TEST windows do not overlap.
TRAIN is reserved for a future learner; no model is fitted now. Selection is frozen
before TEST and can choose NO_TRADE. The fixed baseline is the default trusted ORB
configuration at the same timeframe; ranking compares it with the requested versions.
Both arms reset identical capital each TEST window. Mean-window returns, net expectancy,
profit factor, commissions, entry turnover, per-window equity drawdown, stability and
paired return differences are reported; returns are not presented as one continuous
portfolio. Fewer than three TEST windows means insufficient paired uncertainty evidence.

The returned `comparison_id` references an immutable report:
`GET /strategy-lab/comparisons/{comparison_id}`. Its input OHLCV snapshot is available
through `GET /strategy-lab/comparisons/{comparison_id}/data`, with canonical float64
UTC data, named index, schema and SHA-256 checksum. Reports and inputs persist atomically
in V22 tables. Repeating the same frozen-policy request returns the original report
without fetching changed data. Monitoring GETs never invoke AI/providers, execute
cycles, approve strategies or create orders.

`learning_model_enhanced` and `regime_conditioned_selection` are UNAVAILABLE until a
versioned point-in-time feature/label dataset and categorical regime series exist.
Retrieved historical bars do not prove their original publication vintage or complete
corporate-action coverage. Candidate design may already have used this history.
Consequently reports remain RETROSPECTIVE_EXPLORATORY / NOT_ESTABLISHED even when
one arm wins. Normal intervals and catalog multiplicity corrections do not account
for all dependence or undisclosed historical experiments. Next evidence gate: a
preregistered matched prospective experiment and versioned feature/label capture,
then an advisory supervised baseline with out-of-sample checks, drift and rollback.

Tests use isolated databases. Applying V22 to an existing user database requires
the usual backup/migration operating procedure; development validation does not
modify or restart that database or its running services.

### Prospective decision evidence and model readiness

V23 adds four tables: `prospective_decisions`, `prospective_attachments`,
`prospective_datasets` and `prospective_membership`. Back up an existing database
and apply the explicit migration procedure above before starting this version.
There is **no historical decision backfill**; older assessments and comparisons
are not relabeled as prospective evidence. This development validation uses
isolated databases and does not migrate the operator's database or restart services.

When candidate assessment is configured, newly persisted assessments/opportunities
and their decision records share one transaction. This includes selected strategies,
NO_TRADE and unavailable AI. A capture failure prevents a persisted usable opportunity.
Actual `decision_at` is the post-assessment capture clock, not the earlier research
`as_of`. Records preserve both, all source observations/retrieval timestamps,
assessment/context, the frozen trusted catalog and parameter hashes, ranking policy/
scores and configured PAPER risk/sizing assumptions. Catalog changes during assessment
reject capture. Research is never a risk approval.

Feature schema `decision-research-features-v1` contains `scanner_score`,
`evidence_completeness`, `data_quality`, `volume`, `momentum`, `regime`, `news`,
`fundamental` and `sector`. Scores require matching VALID timestamped source evidence;
missing values remain null with reasons, not invented observations. Feature availability
is no earlier than snapshot creation and relevant source retrieval. Future observations,
future retrievals and future assessments cannot enter earlier decisions. This freezes
available research inputs, not independently certified vendor vintages or a new raw
OHLCV archive. Source responses may still have undisclosed revision history.

All routes below require the existing bearer token:

| Route | Behavior |
|---|---|
| `GET /intelligence/prospective/decisions?limit=100` | Frozen decisions, latest bounded response |
| `GET /intelligence/prospective/decisions/{decision_id}` | Decision, collected attachments, labels/censoring |
| `POST /intelligence/prospective/reconcile` | Explicit idempotent PAPER source replay; never submits orders |
| `GET /intelligence/prospective/readiness` | Read-only current-pool readiness and exact missing feature counts |
| `POST /intelligence/prospective/datasets` | Explicit PAPER reconciliation and immutable dataset creation |
| `GET /intelligence/prospective/datasets/{manifest_id}` | Frozen manifest, hashes, exclusions and readiness |

GETs never fetch providers, reconcile, execute research or change orders. Collection
is explicit: invoke reconciliation after paper activity or recovery before inspecting
updated follow-up evidence. Each attachment exposes its collection timestamp.
Generation/risk records link through the opportunity; entry executions must match
the frozen strategy version, hash and timeframe. Persisted exit proposals expose
partial exits before full closure. Immutable execution/events/fills/outcomes cannot
change unnoticed after capture; mutable order states append new snapshots.
Duplicates/restart replay do not multiply attachments or labels.

Label schema `selected-paper-episode-net-return-v1` is fully closed episode
`net_pnl / entry_notional`, with `profitable = net_pnl > 0`. Fees are already in net
PnL; spread/slippage embedded in fills are not subtracted twice. Complete consistent
attribution, ownership, finite accounting and ordered open/close/known timestamps
are required. Mixed/shared/predecision/version-mismatched episodes are excluded.
NO_TRADE, missing execution, risk rejection, unfilled/rejected orders and fills
awaiting closure are censored, **not zero-return labels**. Unsubmitted alternatives
have no counterfactual label.

Dataset JSON request (dates are illustrative; use completed real collection windows):

```json
{
  "as_of": "2027-06-01T00:00:00Z",
  "train_end": "2027-02-01T00:00:00Z",
  "validation_end": "2027-03-01T00:00:00Z",
  "test_end": "2027-04-01T00:00:00Z",
  "final_end": "2027-05-01T00:00:00Z",
  "embargo_hours": 24
}
```

Boundaries must be timezone-aware, strictly chronological, completed and no later
than the service clock. Embargo is 1-720 hours. Decisions divide into TRAIN,
VALIDATION, TEST and FINAL_HOLDOUT with embargo gaps; the episode must close and
its label must be known by its partition's end. Complete finite features, exact
feature/label schemas and matching feature hashes are required. Synthetic/fixture
provider captures are excluded. Existing same-instrument reserved holdout windows
cannot enter development partitions.

The observation pool remains separate from manifests. Assignment is exclusive:
once a decision belongs to any frozen manifest it cannot be reassigned to a later
training/validation/test set. Final membership is locked review-only, never tuning/
training data. Treat final contents as restricted review evidence, not development
input. A repeated identical policy returns the original manifest unchanged, even if
more outcomes arrive later. Manifests contain actual creation time, policy, frozen rows,
decision/feature/row/dataset hashes and explicit exclusions. No model is trained.

Readiness remains NOT_READY / ADVISORY_ONLY in v1. Necessary sample checks are
100 TRAIN, 30 VALIDATION and 30 TEST labeled decisions with both label classes,
not sufficient evidence of predictive benefit. Additional blockers include source
publication/vintage and real-origin attestation, missing timestamped features,
selected-executed-only selection bias and absent preregistered prospective controls.
Fixture provider names are a rejection safeguard, **not proof that other provider
names represent real data**; all captures remain independently unattested.

Reproducible deterministic tests run the real HTTP/risk/paper path: BUY 100 at $102,
cash $100,000 -> $89,800, exit at $100, cash $99,800, one net -$200 outcome and
net-return label -0.0196078431372549. Decision bytes remain unchanged; restart replay
adds zero attachments. A separate bounded-liquidity fixture verifies 40+60 entry
and exit fills, no premature label and exactly one complete episode. These fixtures
prove mechanics only. In this example readiness specifically lacks `news`,
`fundamental` and `sector`; it also rejects fixture origin and insufficient real samples.

Immutability is application-enforced insert-only storage with checksummed reads,
not a tamper-proof database or protection against a DBA rewriting rows and hashes.
Retain the single-worker operating model. Reconciliation/readiness scan local history
and are not certified for large datasets or concurrent multi-process PostgreSQL use.
No live capability, strategy auto-promotion, new dashboard or change to RiskEngine,
OrderManager, PaperBroker, kill switch or recovery authority is introduced.

Entries need liquidity, slippage, correlation and sector inputs. The API now
derives ADV, expected slippage and correlated exposure from completed daily
bars (`core/market_stats.py`) and sector from the instrument master; if any
input is missing, entries fail closed with the existing `*_UNKNOWN` reasons.
Use `config/instruments_us_paper.json` (via `INSTRUMENT_MASTER_FILE`) or another
reviewed master, a separate database, and the VS Code task
"Start US Paper Auto-Loop API". When AI research is configured, startup also
requires `RESEARCH_SESSIONS` for the loop market (fail closed otherwise).

With `KILL_SWITCH_AUTO=true`, the auto monitor still enforces daily-loss,
drawdown, broker-error, abnormal-move, unexpected-position and health triggers.
While the loop is enabled, it skips the continuous-feed `STALE_MARKET_DATA`
trigger: a polling loop receives no data between ticks, so that trigger would
fire within seconds of every session open. Per-order quote freshness
(`ResilientProvider` validation and RiskEngine `max_market_data_age`) still
blocks any entry on stale data.

Limitations: fills are simulated against quotes (no queue, partial-fill or
market-impact realism); Twelve Data free-plan pacing and daily credits limit
universe size and cadence (the US task uses a 10-minute interval and `TOP_N` 2;
see the cadence note below); every
cycle may spend AI tokens; the token budget and OrderManager remain
process-local. Account submission claims are durable, but this is still a
single-worker application, not a validated multi-worker execution system.
Actual reported AI usage above the configured token budget invalidates that
response; it never supplies fallback trading advice. Budgets reset on restart.

Cadence note (Oct 2026 audit). ORB/VWAP evaluates only the latest completed
5-minute bar, so a scan that does not land on the volume-spike bar misses the
signal. A Twelve Data replay of 10 US symbols (15 Jul–13 Oct 2026, 640
symbol-days) found the first BUY signal on 196 days at a 5-minute cadence, 133 at 10
minutes (mean delay 28.6 min), 100 at 15 minutes (42.4 min) and 64 at 30 minutes
(58.4 min). Each scan uses about 16 Twelve Data credits (bars plus a quote per
eligible symbol, no cache). The basic plan allows 8 requests per minute and 800
credits per day, so 5 minutes (~1,250 credits/day) is not viable. 10 minutes
(~39 scans, ~625 credits) leaves limited headroom for protective-monitor and
execution quotes. Check `https://api.twelvedata.com/api_usage` before widening
the universe or adding manual scans. Exhausted credits fail closed: provider
errors block entries, and repeated failures stop the loop in exits-only mode.
A profitable or unprofitable run verifies the plumbing and rule behaviour, not
strategy edge; judge the strategy only with proper backtests and many sessions.

## Docker Compose

The sample stack runs a single paper API process and PostgreSQL. Before startup:

1. Copy `.env.example` to an ignored local `.env`; supply the required
   `ENTRY_WINDOW_START`, `ENTRY_WINDOW_END`, `MAX_POSITION_QUANTITY`, and
   `MAX_ORDER_NOTIONAL` values.
2. Create the secret files referenced by Compose under `secrets/`:
   `db_password.txt`, `database_url.txt`, `api_token.txt`, and
   `ai_api_key.txt`. Use unique, generated values and restrictive file
   permissions. The AI key file may be empty while AI research is disabled; if
   enabling it, put the provider key there and set `AI_BASE_URL`, `AI_MODEL`,
   and `RESEARCH_SESSIONS` in the ignored `.env`. Do not commit these files.
3. Review the local port binding and persistence/backup location.
4. Start with `docker compose up --build`. The migration service runs before the
   API; application startup does not auto-migrate in this stack.

The optional `backup` profile writes PostgreSQL custom-format dumps to the
Compose `backups` volume and prunes older dumps. It is not an off-host backup.
Copy backups to an independently protected location and periodically verify a
restore in an isolated environment.

## Migration, backup, recovery

- Check schema state with `python -m stockmarket.ops status`; a non-zero status
  indicates pending migrations.
- Apply migrations during a controlled maintenance window with
  `python -m stockmarket.ops migrate`. Staging and production bootstrap are
  configured not to migrate automatically.
- Create a database backup with
  `python -m stockmarket.ops backup --dir <protected-backup-directory> --keep 14`.
  PostgreSQL backup requires `pg_dump` on the host; SQLite uses its online
  backup API. Protect backup files as sensitive trading/account data.
- Restore into a new or isolated database first. For PostgreSQL custom dumps,
  use `pg_restore` with the intended database and least-privilege operator;
  for SQLite, preserve the original file and copy the verified backup into the
  configured location only while the service is stopped.
- Start the application against the restored database, run `ops status` and
  `ops verify-audit`, and inspect portfolio/order state before resuming paper
  activity. Do not overwrite the only copy of existing state during recovery.

## Health and security checks

- `/health` is unauthenticated and reports service health; protected operational
  data and mutation endpoints require the bearer token.
- Run the repository checks locally with
  `python -m unittest discover -s tests -v`,
  `bandit -r src/stockmarket -c bandit.yaml -ll`, and
  `pip-audit -r requirements.txt`. CI runs these checks on pushes and pull
  requests.
- Rotate API/database credentials after suspected exposure. Do not put tokens
  in command history, logs, screenshots, issue reports, or dashboard output.
- Treat Yahoo Finance and mock market data as research-only. No feed in this
  stack is guaranteed execution-grade.

## Known operational limitations

- This setup has no live broker adapter, live-order path, kill-switch alert
  delivery guarantee, high-availability deployment, or tested disaster
  recovery objective.
- The API process keeps runtime order and gate state in memory and is limited to
  one worker.
- PAPER cycle runs and executor/fill records are durable. Cycles and
  position-exit evaluations are invoked manually unless the opt-in, paper-only
  `PAPER_AUTO_LOOP` is enabled (see above); it is an in-process local
  verification aid, not unattended-operation evidence or a production
  scheduler. Active-order
  reservations are process-local; multi-worker order submission remains
  unsupported.
- The repository does not bundle AI/Finnhub credentials/configuration, research-
  session policy, or NSE/Finnhub symbol mappings by default. The generic
  OpenAI-compatible adapter and Finnhub news adapter are opt-in. Yahoo's
  opt-in fundamentals adapter is limited to dated earnings events; NSE sector
  evidence is research-only and limited to operator-reviewed symbol/index
  mappings. No provider is guaranteed to provide execution-grade data.
- Exchange calendars outside verified coverage must fail closed. US and Xetra
  currently have 2026 data only. India remains calendar-uncovered until NSE and
  BSE official schedules are both verified and reconciled. Verify calendar
  updates against the relevant exchange before relying on session decisions.
- The legacy Streamlit applications persist local paper state separately from
  the platform database; back up and migrate those files only through an
  explicit, reviewed process.
# Personal recommendation-only setup

For an explicitly populated development universe, data diagnostics, deterministic
signals and optional AI-supported recommendations without any paper/live broker
execution, see [Personal Research Setup](PERSONAL_RESEARCH.md). This mode requires
`PERSONAL_RESEARCH=true` and an explicit master. Backend market profiles supply
default research sessions; dashboard selection switches countries without
changing environment variables. `MARKETS` sets the Auto tie-break/fallback
preference and `BASE_CURRENCY` remains an execution-account setting, not a
research-country switch. Optional `RESEARCH_SESSIONS` overrides policy only;
calendar validation remains mandatory. Migration
V15 persists advisory runs; existing execution/recovery workflows below apply
only when personal mode is disabled and instrument metadata has been verified.
# Preregistering a calendar-day PAPER experiment

This increment adds protocol registration, **not a complete experiment runner**.
Do not count earlier trades as a preregistered experiment or treat registration
as permission to trade. Existing scanner, research, strategy eligibility, risk,
execution and protective-monitor controls remain authoritative.

Migration **V27** adds `prospective_experiments`, using the existing prospective
repository's immutable identity and checksummed-read pattern. It does not backfill
history. Apply it only to an isolated development database first. Updating an
operator database and restarting its worker require separate approval and the
existing backup/migration procedure; `AUTO_MIGRATE=false` startup will refuse a
database without V27. No running operator database was migrated for this increment.

After approved migration/source alignment:

1. Review a written protocol defining benchmark, strategy baselines, costs,
   model/prediction scoring and acceptance criteria. No arbitrary profitability
   thresholds or automatic model promotions are provided.
2. Choose a **future market-local date**. The authenticated request below defaults
   to 30 calendar days, including weekends/holidays, from local midnight through
   the exclusive midnight after day 30. DST is handled in the market timezone.
   Supply a real benchmark instrument already in the canonical runtime master,
   in the same market/account currency; no symbol substitution or implicit FX.
3. `POST /paper/experiments` with a JSON body such as:
   ```json
   {
     "experiment_id": "us-paper-reviewed-001",
     "start_date": "<future YYYY-MM-DD>",
     "duration_days": 30,
     "benchmark_instrument_id": "<canonical benchmark instrument ID>",
     "operator": "<operator>",
     "acceptance_policy_ref": "<reviewed protocol document/version>"
   }
   ```
   Use the same bearer authentication as other paper API calls. Registration
   requires the configured single-market paper loop and research services. It
   freezes universe definition/membership, benchmark metadata, registered strategy
   versions/configuration fingerprints, advisory AI provider/model identifier,
   risk settings, cadence, selection mode and data-provider configuration.
   An operator-supplied policy reference is **not independently verified approval**.
4. `GET /paper/experiments/{experiment_id}` reads the saved protocol without
   running research or placing orders. Repeating identical registration is
   idempotent; changing a registered protocol under the same ID is rejected.
   Protocol contents are immutable through this API and checksummed on reads,
   but are not proof against privileged database tampering.
5. Status is `SCHEDULED`, `OBSERVATION_WINDOW`, or `EVALUATION_DUE`, derived from
   the clock. These are **calendar phases**, not worker/collection readiness.
   End-of-window is never mislabeled completed evaluation.

**Still blocked for a certified 30-day experiment:** binding each scheduled
cycle/decision to the registered protocol and rejecting configuration drift;
explicit existing-position/cash baseline; verified cost/fill assumptions;
daily atomic financial/equity/exposure snapshots; matched benchmark prices and
strategy-baseline returns; daily/final reports and separate prediction-quality
metrics. Registration does not start/stop the worker or enforce its end date.
Missing quotes and market closures must remain explicit missing observations,
not forward-filled performance. Existing prospective decisions/attachments,
order/fill history, outcome episodes and immutable datasets must be reused for
these next phases rather than copied into a competing ledger.
