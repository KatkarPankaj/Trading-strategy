# Platform operations

The October platform is paper-trading software, not a live execution service or a
production-readiness claim. Keep `TRADING_MODE=paper` and
`ENABLE_LIVE_TRADING=false`. The checked-in Compose stack pins those values.
Do not expose the API directly to the public internet; the provided Compose
binding is loopback-only and remote access requires a separately secured TLS
terminator and network policy.

## Local API

1. Install the dependencies listed in `requirements.txt` in a Python 3.10+
   environment.
2. Set the required risk-window and order-limit environment variables described
   in `.env.example`. Set `DATABASE_URL` to a local SQLite file or a PostgreSQL
   URL; credentials belong in environment variables or secret files, never in
   committed configuration.
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
`AI_MAX_OUTPUT_TOKENS` (1-32768) is sent as `max_tokens`, which some reasoning
models reject. Each persisted assessment records `ai_usage` (calls, attempts,
prompt/completion counts, and whether counts were estimated).
Compose mounts `secrets/ai_api_key.txt` as the key-file secret. Keep the
non-secret endpoint, model, and session settings in the ignored host `.env`.

The API-backed Streamlit dashboard includes advisory research, ranked
opportunities, paper-order review, and explicit proposal acceptance. Before
submitting a proposal it checks that `/health` reports `PAPER`; the API
independently enforces PAPER mode, proposal freshness, and RiskEngine approval.
The legacy dashboards and their local files are not changed by this UI.
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
| `PAPER_AUTO_LOOP_TOP_N` | `3` | capped by the autonomous cycle bound |
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
Unfinished cycles are recovered at startup. PARTIAL/FAILED cycles and startup
recovery failures count towards the failure limit; a stopped loop cannot submit
even through a direct tick. Resolve the reported failure before restarting the
API. `GET /paper/loop` (bearer token) reports state, last result, failure counts
and per-cycle startup recovery failures.

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
universe size and cadence (prefer a 30-minute interval and `TOP_N` 1–2); every
cycle may spend AI tokens; the token budget and OrderManager remain
process-local. Account submission claims are durable, but this is still a
single-worker application, not a validated multi-worker execution system.
Actual reported AI usage above the configured token budget invalidates that
response; it never supplies fallback trading advice. Budgets reset on restart.
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
