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
`AI_TIMEOUT_SECONDS` defaults to 20 and is bounded to 1-120 seconds.
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

When AI is configured, set `RESEARCH_SESSIONS` to a JSON object keyed by
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
- PAPER cycle runs and executor/fill records are durable, but cycles and
  position-exit evaluations are invoked manually. There is no scheduler,
  unattended-operation evidence, or production-readiness claim. Active-order
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
`PERSONAL_RESEARCH=true`, an explicit master, and per-market sessions. Migration
V15 persists advisory runs; existing execution/recovery workflows below apply
only when personal mode is disabled and instrument metadata has been verified.
