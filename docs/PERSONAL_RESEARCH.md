# Personal research: recommendations, not execution

The workflow is instrument master -> market data -> registered deterministic
strategy -> existing candidate research -> optional existing AI assessment ->
deterministic opportunity ranking -> BUY / SHORT / AVOID -> human decision.
No profitability or production-readiness claim is made.

## Why the registry was empty

`GET /instruments` reads the instrument repository. API startup previously loaded
that repository into the scanner/provider maps without importing any master.
`RegistryUniverseProvider` only indexes those records; it does not discover
stocks. An empty database therefore produced empty selectors. A quote provider
does not populate the instrument master, and the mock provider has no default
prices or bars.

Startup now supports an explicit bounded master through the existing
`Instrument`, market factory and instrument repository:

- `INSTRUMENT_BOOTSTRAP=development` loads
  [development_instruments.json](../src/stockmarket/core/development_instruments.json).
  This contains 10 US equity/ETF records and 6 India equity records, canonical
  MIC:symbol identities and explicit research-provider symbols.
- `INSTRUMENT_MASTER_FILE` imports a local JSON array of 1-1000 records instead.
  Required fields: `symbol`, `market`, `mic`, `asset_class`, `tick_size`.
  Optional fields: `lot_size`, `provider_symbol`, `active`, `tradable`,
  `shortable`, `name`. Markets supply currency, timezone and regular hours.
  An empty response, duplicate identity, invalid metadata or unreadable file
  fails startup visibly. Validation occurs before transactional insertion.
- Default `INSTRUMENT_BOOTSTRAP=none` preserves existing deployments and reports
  `NO_MASTER_CONFIGURED`. Existing rows are never overwritten by an import.

This is not an authoritative exchange master or a complete global universe.
Development tick/lot and shortability assumptions are provisional; shorts are
disabled. The development profile requires `PERSONAL_RESEARCH=true`.
Persisted development-named metadata also prevents subsequent startup in
execution mode; use a separate verified registry for paper execution.
Inactive/untradable records remain registered but are rejected by the scanner.
The development universe alias is bounded to each market's registered
records; preserved operator records are included, not silently replaced.
Restart the API after importing metadata so in-memory service maps are refreshed.

US and India are supplied as development universes. Germany remains an existing
market definition and can use a supplied master. US and Xetra have 2026 calendar
data; India has no covered holiday years. India scans currently reject candidates
with an explicit unsupported-calendar diagnostic. Session configuration is not
holiday-calendar coverage and does not bypass this rejection.

## Select a country in the dashboard

The Market selector offers Auto and all backend-registered profiles, currently
India (NSE/BSE), US (NYSE/NASDAQ/NYSE Arca), and Germany (Xetra).
Normal switching does not require changes to `MARKETS`, `BASE_CURRENCY`, or
`RESEARCH_SESSIONS`, an API restart, or a separate country database.
Personal mode imports all registered markets from the configured master.

[MarketProfileService](../src/stockmarket/core/market_profiles.py) resolves
exchange-local time, calendar coverage, currency, session, strategy support and
single-market universe choices. It delegates calendar/session decisions to the
existing market definitions. The dashboard does not implement exchange clocks.
Auto prefers regular OPEN, then PRE_MARKET, then POST_MARKET. Ties prefer the
first configured `MARKETS` entry, then a stable market code. If none is active,
it displays that default's actual CLOSED, HOLIDAY or UNSUPPORTED_CALENDAR status.
Manual selection never switches to another country.

US and Xetra calendar files cover 2026 only; uncovered exchange-local years
report UNSUPPORTED_CALENDAR, including weekends in an uncovered year.
India remains unsupported until a verified calendar is supplied.
Germany's covered 2026 sessions can be displayed, but the development master
contains no German instruments. Its empty universe is explicit, with no
fabricated recommendation. Auto selects by session, not by whether a master has
instruments; an open Germany session can therefore resolve to an empty universe.

Research policy defaults use registered open/close/timezone facts: opening range
15 minutes; India entry 09:45, cutoff 15:00, square-off 15:20; US entry 09:45,
cutoff 15:00, square-off 15:55; Xetra entry 09:15, cutoff 16:30, square-off 17:25.
Optional `RESEARCH_SESSIONS` entries override only the named profile's research
policy. They never manufacture holiday coverage or override an early close.
Future countries require a market definition, verified calendar, reviewed
research policy and instrument master; the UI discovers their backend labels.

Selection changes research context, not the persisted execution account's base
currency, cash, risk limits or holdings. There is no FX conversion assumption.
AI configuration remains separate and optional; its status is shown before
research. Missing AI does not block instruments, data, signals or diagnostics.

## Start on Windows

Use an interpreter with the existing requirements installed. These commands
assume the repository virtual environment; the VS Code tasks use the selected
interpreter instead. Configure an API bearer token privately in
`secrets\local_dashboard.secret`; never put a token or AI key in committed files.
The application does not automatically load `.env` for these terminal commands.

In the API terminal:

```powershell
$env:PYTHONPATH = 'src'
$env:APP_ENV = 'development'
$env:TRADING_MODE = 'paper'
$env:ENABLE_LIVE_TRADING = 'false'
$env:PERSONAL_RESEARCH = 'true'
$env:INSTRUMENT_BOOTSTRAP = 'development'
$env:MARKETS = 'IN'
$env:BASE_CURRENCY = 'INR'
$env:DATA_PROVIDER = 'yahoo'
$env:DATABASE_URL = 'sqlite:///outputs/paper_india.db'
$env:API_TOKEN_FILE = 'secrets\local_dashboard.secret'
$env:ENTRY_WINDOW_START = '09:45'
$env:ENTRY_WINDOW_END = '15:00'
$env:MAX_POSITION_QUANTITY = '20'
$env:MAX_ORDER_NOTIONAL = '25000'
.\.venv\Scripts\python.exe -m uvicorn stockmarket.api.bootstrap:create_app_from_env --factory --host 127.0.0.1 --port 8000 --workers 1
```

The existing bootstrap still validates entry/risk settings, even though execution
is disabled. The values above preserve the selected India 09:45, quantity 20 and
INR 25,000 limits; they do not authorize orders. Simulated starting cash defaults
to INR 100,000, separately from the max-order-notional limit.

`MARKETS=IN` and `BASE_CURRENCY=INR` above preserve the existing default/account
settings, not a restriction to India. Both may be omitted for a new setup
(defaults US/USD). Leave them unchanged when selecting US or Germany in the UI.
No `RESEARCH_SESSIONS` setting is required in personal mode, with or without AI.

These times are instrument-local, not fixed UTC. Only covered trading sessions
and valid, completed, fresh intraday data can produce a strategy setup.

In the dashboard terminal:

```powershell
$env:PYTHONPATH = 'src'
$env:API_BASE_URL = 'http://127.0.0.1:8000'
$env:API_TOKEN_FILE = 'secrets\local_dashboard.secret'
.\.venv\Scripts\python.exe -m streamlit run dashboard_app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

Open <http://127.0.0.1:8501/>. VS Code tasks `Start Personal Research API` and
`Start platform PAPER dashboard` launch the same international research setup
while preserving the existing India account/default. They use the selected
Python interpreter without committing secret values.

## API and dashboard workflow

All research endpoints below require the configured bearer token. Health is
public and reports PAPER trading mode plus PERSONAL_RESEARCH application mode,
execution disabled, instrument count and whether AI assessment is configured.

```powershell
$headers = @{Authorization = 'Bearer ' + (Get-Content 'secrets\local_dashboard.secret' -Raw).Trim()}
Invoke-RestMethod 'http://127.0.0.1:8000/health'
Invoke-RestMethod 'http://127.0.0.1:8000/instruments' -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8000/universes' -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8000/markets/status?selected_market=AUTO' -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8000/research/personal/status' -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8000/research/personal/diagnostics/XNSE:RELIANCE' -Headers $headers
$body = @{selected_market='IN'; top_n=5; idempotency_key=[guid]::NewGuid().ToString('N')} | ConvertTo-Json
Invoke-RestMethod 'http://127.0.0.1:8000/research/personal/runs' -Method Post -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 300
Invoke-RestMethod 'http://127.0.0.1:8000/research/personal/runs?limit=10' -Headers $headers
```

Alternatively, run the scanner alone with authenticated `POST /scanner/scan`
and body `{"universe_id":"IN_ALL","mode":"RESEARCH","top_n":5}`.
Its rejection records remain accessible through the existing scanner routes.

`GET /markets/status?selected_market=AUTO|IN|US|DE` returns selected/resolved
market, exchange/MICs, currency, timezone, session/status, local/as-of timestamps,
calendar coverage, all profile labels, research policy, strategy/universe choices,
provider, limitations and AI configuration status. Unknown market codes return
404. `GET /instruments?market=US` filters instruments regardless of the default
country in personal mode. `GET /markets` retains its market-list envelope.

`POST /research/personal/runs` accepts optional `selected_market`; omitting
`universe_id` chooses the profile default. A supplied universe must belong only
to the resolved country. A change of Auto resolution between display and run
fails visibly rather than scanning a different country's supplied universe.
Runs persist the resolved profile at the research `as_of` boundary. Idempotent
replay uses that saved resolution, even if clocks or profiles have changed.
Older requests supplying only `universe_id` remain compatible.

The dashboard Research tab offers Market and universe/registered-strategy selection,
individual data diagnostics and bounded research runs. Opportunities displays
persisted runs after refresh/restart. Each row includes rank, direction, score,
strategy, quality, AI status, risk flags and reasons. Expand details for entry,
stop, target, reward/risk, signal/version, snapshot, news/other evidence, regime,
AI model/version and timestamps. Scores are ranking scores, not profit odds.

The personal service has no broker, order manager or trading-service dependency.
Execution API mutations, cycle recovery, cancellation and recovery/kill-switch
mutations are blocked in this mode; the entry gate is halted and no cycle service
is exposed. Legacy paper services and routes retain their behavior outside it.
Every recommendation is labelled `RECOMMENDATION ONLY`,
`execution=NOT_SUBMITTED`, `risk_status=NOT_FINAL_EXECUTION_AUTHORITY`.

Defaults evaluate at the last five-minute decision boundary. The raw response
is validated against the requested range before incomplete bars are removed;
only bars completed by `as_of` reach ORB/VWAP. Optional `as_of` must be aware and
not future. Historical data can be revised by its provider; this is not a claim
of revision-free point-in-time fundamentals or quotes. Historical quote requests
are explicitly unsupported, not replaced with current prices.

At most 10 selected candidates per personal request are assessed sequentially.
Missing critical technical/regime evidence, unsupported sessions, invalid prices,
HOLD/NO_SIGNAL, non-shortable instruments or incompatible/unavailable AI withhold
BUY/SHORT and retain AVOID with reasons. Available raw deterministic signals remain
visible even without AI. Missing optional evidence is retained and penalized by
the existing assessment/ranking rules, never invented.

Migration V15 stores complete personal runs and recommendation provenance.
An identical idempotency key/request replays the persisted result after restart
without new provider/AI calls. Changed requests with that key conflict. An
interrupted RUNNING record is not reported as successful; inspect it and use a
new key. FAILED/PARTIAL results are durable. This is local single-worker
composition, not a distributed scheduler or execution authority.

## Enable AI, optionally

No AI key is required for master import, quotes/OHLCV, scanner, deterministic
strategies or candidate snapshots. Final AI-supported BUY/SHORT recommendations
require a configured assessment provider. Without it the UI explicitly says
`CONFIGURATION_MISSING`; raw strategy signals are retained but final actionable
recommendations are withheld.

The existing adapter supports **OpenAI-compatible chat-completions endpoints**:

```powershell
$env:AI_BASE_URL = 'https://api.openai.com/v1'
$env:AI_MODEL = 'YOUR_APPROVED_MODEL'
$env:AI_API_KEY_FILE = 'secrets\ai_api_key.txt'
$env:AI_TIMEOUT_SECONDS = '20'
```

Create a key in your chosen provider's account/API console, confirm API billing,
model access and endpoint compatibility, and save it privately. `AI_API_KEY` is
the environment-variable alternative. Copilot/ChatGPT subscriptions do not supply
an app API key. A native Anthropic/Claude endpoint is **not** supported by this
adapter; do not paste a Claude key into an OpenAI endpoint. Revoke any exposed key.
No previously pasted key is used by this workflow.

Personal mode supplies research sessions whether or not AI is configured;
legacy AI research still requires explicit `RESEARCH_SESSIONS`. The request
timeout defaults to 20 seconds and is bounded to 1-120 seconds. The existing
adapter does not implement AI quota-aware retries or a separate rate limiter;
provider errors/rate limits result in AI_UNAVAILABLE and AVOID, not fallback
trades. Two calls may be made per assessed candidate (assessment, registered
strategy selection). The dashboard bounds personal requests to 300 seconds; a
timeout can leave a server-side run finishing, so inspect persisted runs before
starting another. Start with a small Top-N to control cost/latency.

News is separately optional: `NEWS_PROVIDER=finnhub` with `FINNHUB_API_KEY` or its
`_FILE` alternative and explicit `FINNHUB_SYMBOL_MAP` where necessary. India news
coverage is not guaranteed. `FUNDAMENTAL_PROVIDER=yahoo` adds timestamped
reported-EPS evidence, not full fundamentals. Sector adapters require reviewed
mapping. Missing components are explicitly MISSING/UNAVAILABLE/REJECTED.
Yahoo is research-only, not an authoritative master or execution feed.

## Troubleshooting and verification

- Zero registry: inspect `/research/personal/status` for master source, requested,
  inserted/preserved counts and `empty_reason`. Configure a master, check enabled
  markets and restart API. The UI has no hard-coded stock list.
- Registered but no candidates: inspect persisted scanner diagnostics for
  inactive/untradable metadata, unsupported calendar, stale/missing/invalid
  bars, provider errors or insufficient history. Registry count is not a count
  of eligible trade setups.
- Data diagnostics distinguish AVAILABLE, STALE, MISSING, INVALID and
  UNSUPPORTED, with latest bar/quote timestamps, currency, session and quality.
- Mock has no startup price fixtures: MISSING is expected. Yahoo network,
  availability and timestamps must be verified rather than assumed.
- Future data/request: rejected before strategy evaluation; no synthetic zero
  prices. No configured strategy/session/AI is not a successful trade signal.
- Automated focused tests exercise master validation/preservation, completed
  bars, stale/future rejection, optional AI, AI-vs-HOLD/direction alignment,
  ranking/provenance, replay and execution guards with offline fixtures.
  AI fixtures are test-only and are not evidence of real provider access.

Real India end-to-end signals remain blocked until verified calendar coverage
and fresh usable market data are supplied. Missing credentials do not disappear
merely because the UI can show instruments. No orders should be created by any
personal research run; check orders/fills independently when verifying.

### Local verification, 2026-10-08

- Restarted API and dashboard from the VS Code tasks and verified responsive
  health endpoints and the personal-mode banner.
- India master requested 6 records, inserted SBIN and preserved 5 operator
  records. `/instruments` returned all 6 canonical NSE identities.
- Real Yahoo OHLCV was AVAILABLE for all 6, with 259-262 five-minute bars and
  latest timestamps around 07:15 UTC. INFY's quote was AVAILABLE; the other
  sampled quotes were rejected as `PRICE_OFF_TICK`. Metadata/provider precision
  was not silently adjusted to pass validation.
- India research returned a durable FAILED/no-eligible-candidates result:
  calendar coverage is absent; preserved operator records are also non-tradable.
  Idempotent replay returned the same run. Dashboard displayed that failure,
  not fabricated recommendations.
- Order count was zero before and after research; execution remained
  NOT_SUBMITTED. A valid API order request was blocked by personal mode.
- Offline tests exercised a covered US fixture through deterministic BUY,
  research snapshot, test-only AI assessment and existing opportunity ranking,
  with no orders/fills. Real AI credentials were not configured or used.

Automated validation uses `PYTHONPATH=src` and
`python -m unittest discover -s tests`. The suite also emits expected
rejected-case logs and existing AnyIO resource warnings; those are not evidence
of a tested live broker or of production readiness.

### International market-selector verification, 2026-10-08

- 17 new focused tests cover profiles, Auto/manual selection, DST mismatch weeks,
  holidays, early closes, exchange-local year coverage, missing calendars, future
  registry markets, universe isolation, optional AI, API authentication, saved
  Auto replay, session overrides and dashboard request wiring.
- 40 related tests and the full 353-test unittest suite passed. Changed Python
  files reported no editor diagnostics; changed signatures retained compatible
  callers, and `git diff --check` passed.
- Restarted both VS Code tasks. API/dashboard health endpoints responded, with
  16 registered US/India instruments and execution disabled.
- At verification time Auto resolved open Xetra; explicit US remained CLOSED
  instead of switching to Germany. Explicit India stayed India and displayed
  UNSUPPORTED_CALENDAR. All four selector choices and country-specific counts,
  currency/timezone/session details and missing-AI messaging were verified.
- India dashboard research and Germany API research persisted FAILED/
  no-eligible-candidates outcomes, not fabricated recommendations. Orders and
  fills stayed zero before and after; the order mutation endpoint returned 403.
- AI-configured behavior was verified with offline fixtures/configuration, not
  real credentials or a live model request.
