# Global Market Research and Paper-Trading Platform

## Quick start: current US PAPER dashboard (Windows / VS Code)

1. Open this repository in VS Code and use **Python: Select Interpreter** to
   select the environment with the project dependencies installed.
2. Check that ports 8000 and 8501 are not already serving this application;
   reuse existing services rather than starting a second API worker.
3. Verify the existing US paper database and local API/provider secret files.
   Do not reset the account or apply migrations just to start the dashboard.
4. Open the command palette (**Ctrl+Shift+P**), select **Tasks: Run Task**, then
   **Start US Paper Auto-Loop API**. Wait for application startup on port 8000.
5. Select **Tasks: Run Task** again, then **Start platform PAPER dashboard**.
6. Open **<http://127.0.0.1:8501/>**. Confirm **PAPER**, **US**, recovery status,
   protective-monitor health and entry-gate diagnostics before expecting orders.

**Safety:** the existing US task enables automatic **exploratory/unvalidated
paper** trading when all independent gates permit it. Starting services does not
override a pause, failed recovery, stale data or risk rejection. A `PARTIAL`
recovery keeps new entries blocked and requires investigation, not bypassing.
The task currently uses OpenAI, not Ollama. The dashboard can connect to separate
US and India API endpoints; its **Market API context** selector selects an API
endpoint, not the Market Watch market. India remains personal research only and
cannot execute paper orders.

See [the complete startup procedure](#windows-start-the-current-us-paper-trading-dashboard)
for credential checks, port checks, database precautions and API health commands.

### Optional India research connection

The shared dashboard has an India endpoint configured on port 8001, separate
from the US paper worker on port 8000. To display India research, run **Start
Personal Research API** from **Tasks: Run Task**, then select **IN research
API (execution disabled)** in the dashboard's **Market API context** selector.
This uses the separate `outputs/paper_india.db`; it does not enable India
orders. India execution continues to require reviewed exchange calendars and
validated provider and instrument-mapping evidence. The two workers must remain
on distinct API endpoints and databases.

The API-backed dashboard includes a dedicated **Market Watch** workspace for
US/India canonical-instrument search, a persisted shared watchlist and explicit,
timestamped quote refresh. Enable both observation markets with `MARKETS=US,IN`;
this does not enable simultaneous paper loops or fund INR positions. India
session authorization requires separately reviewed calendar files; no verified
India holiday calendar is bundled. See the [Market Watch operations guide](docs/OPERATIONS.md#market-watch-us-and-india-observation-slice)
for V25 migration, provider mappings, freshness policies and limitations.

The current development path is the October platform layer: provider-aware market
research, deterministic strategy evaluation, risk-gated paper execution, and an
API-backed monitor with explicit paper-proposal acceptance. It remains paper-only
and is not a production/live trading system. The earlier NSE-focused Streamlit simulators and backtest
workflows are retained separately for compatibility; they are not the authority
for new platform architecture.

See [Platform Architecture](docs/ARCHITECTURE.md) for system boundaries and
[Platform Operations](docs/OPERATIONS.md) for API setup, secrets, deployment,
backup, and recovery.

## Evidence-based strategy intelligence

Closed paper outcomes inform a deterministic, version/instrument/timeframe-matched
advisory ranking with sample qualification, net costs, chronological stability,
drawdown and catalog-multiplicity-adjusted uncertainty. Final holdouts remain
independent review evidence and are excluded from research selection/tuning.
Authenticated `/strategy-lab/comparisons` requests persist matched retrospective
fixed-ORB versus ranking-only reports, together with reproducible data snapshots.
Model-enhanced and regime-conditioned arms are explicitly unavailable without
their required timestamped datasets. These reports do not establish predictive
improvement or profitability. See [Evidence operations](docs/OPERATIONS.md#evidence-based-strategy-intelligence).

New candidate assessments also capture an immutable prospective decision ledger:
actual decision time, frozen strategy parameters, point-in-time research features,
ranking evidence, risk assumptions and source provenance. Authenticated reconciliation
attaches subsequent paper events and completed outcomes without changing the decision.
Chronological dataset manifests enforce embargoes, label availability and locked
membership; final holdouts cannot become training data. Supervised readiness remains
NOT_READY / ADVISORY_ONLY until real, verified point-in-time evidence and a prospective
control experiment exist. No model is trained or automatically promoted.
See [Prospective evidence operations](docs/OPERATIONS.md#prospective-decision-evidence-and-model-readiness).

The optional API paper loop has a separately scheduled protective supervisor
(30 seconds by default) with deadline telemetry and a dedicated bounded-wait
quote lane. It monitors pending orders and protective exits during slow research,
cancels pending entries before end-of-day
flattening, and automatically attaches paper events/outcomes to frozen decisions.
Repeated entry failures pause scans without disabling exits. Authenticated entry
pause/resume controls are audited and survive restart; they never clear a kill switch.
Recovery, failed/stale checks and watchdog-detected stalls block new entries.
The latency budget is not a hard guarantee: shared execution/database locks and
non-cancellable provider calls remain limitations.
Entry quotes/statistics are prepared outside the execution lock and checked again
for freshness and changed portfolio/order state before final RiskEngine validation.
Protective monitoring and end-of-day flattening also prepare quotes outside that
lock, then revalidate the snapshot before atomic updates and final exit risk checks.
New entries stay blocked during preparation and after incomplete checks.
History replay validates page ordering, counts and SQLite change revisions; oversized
histories fail closed rather than pretending an incomplete checkpoint is usable.
PostgreSQL remains unvalidated and lacks the snapshot-revision contract needed
by this guarded startup replay.
Provider lanes admit at most four outstanding reads each, including timed-out reads.
SQLite admission/statement waits and historical reads now have explicit fail-closed
bounds; see [execution isolation](docs/OPERATIONS.md#execution-preparation-and-bounded-storage).
Strategies shows prospective evidence/readiness, and System health explains
closed-session/stale-data blockers without granting risk approval.
See [Paper loop operations](docs/OPERATIONS.md#opt-in-paper-auto-loop-local-algorithm-verification).

## Personal research and human decisions

Use `PERSONAL_RESEARCH=true` for a recommendation-only application with no broker
execution, including no paper order submission. An explicit development master
or configurable JSON master populates the existing instrument repository at
startup. The dashboard then uses the existing scanner, deterministic ORB/VWAP
strategy, candidate research and optional AI assessment/ranking. Missing AI does
not disable discovery, data or strategy signals; it withholds final BUY/SHORT
recommendations and is shown explicitly. No recommendation guarantees profit.
Choose Auto, India, US or Germany in the dashboard. Backend profiles resolve
exchange-local sessions and single-country universes without per-switch
environment changes. US/Xetra calendar coverage is limited to 2026; India
remains unsupported and Germany's development universe is empty.

See [Personal Research Setup](docs/PERSONAL_RESEARCH.md) for exact launch commands,
configuration, diagnostics, API workflow and current India calendar limitations.
The older research and paper-execution paths below remain available when this
mode is disabled, using separately verified instrument metadata.

## Product Goal
- Start with a defined budget. Default budget is `Rs 200000`.
- Scan market data and generate actionable intraday buy/sell candidates.
- Simulate trades automatically using paper money only.
- Track current capital, open trades, closed trades, and PnL.
- Learn from past trade outcomes to favor stronger conditions and avoid weaker ones.

## Core Simulator Scope
- Budget setup: total capital, risk per trade, and max trades per day.
- Strategy signals: top intraday candidates from predefined strategy rules.
- Auto trade simulation: paper-only trade execution with portfolio updates.
- Portfolio tracking: capital used, available cash, unrealized/realized PnL.
- Trade history: entry, exit, PnL, and reason for trade.
- Learning feedback: summaries of what worked and what failed.

## What this includes
- Intraday data fetch from Yahoo Finance via `yfinance`.
- NSE/BSE symbols support with Yahoo suffixes:
  - NSE: `RELIANCE.NS`, `TCS.NS`
  - BSE: `RELIANCE.BO`, `TCS.BO`
- Opening Range Breakout strategy with VWAP and volume filters.
- Backtest engine with slippage, commission, stop-loss, take-profit, time-based exits, and end-of-day square-off.
- CLI for backtesting and latest signal inspection.
- CLI parameter sweep for quick tuning and ranking.

## Setup
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Configure
Create `config.json` from the template and adjust values.
```powershell
Copy-Item config.example.json config.json
```

Time handling is controlled by `market_timezone` in `config.json`.
For NSE/BSE use:
```json
"market_timezone": "Asia/Kolkata"
```
This keeps strategy/session times correct even if your Windows region is not India.

## Run backtest
```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m stockmarket.cli backtest --config config.json
```

## Launch browser app
```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run src/stockmarket/webapp.py
```

Then open the local URL shown in terminal (usually http://localhost:8501).

## Launch API-backed platform dashboard
This separate Streamlit page reads data from the platform API. It includes advisory research and opportunity ranking, server-backed order review, and an explicit paper-only proposal acceptance form. It cannot create arbitrary orders or bypass the platform RiskEngine.

The API must be running and `API_TOKEN` must match its configured bearer token. `API_BASE_URL` defaults to `http://127.0.0.1:8000` for local use; non-loopback API URLs must use HTTPS. The legacy advisory `/research` route requires an approved OpenAI-compatible endpoint configured through `AI_BASE_URL`, `AI_MODEL`, and `AI_API_KEY`/`AI_API_KEY_FILE`, plus explicit `RESEARCH_SESSIONS` for each enabled market. Personal Research instead supplies backend market-profile sessions and permits deterministic research without AI. The dashboard does not supply credentials or invent session boundaries; if research components or calendar coverage are unavailable, it reports an error or diagnostic.

```powershell
$env:PYTHONPATH = "src"
# Set API_TOKEN using your approved local secret-handling method.
.\.venv\Scripts\python.exe -m streamlit run dashboard_app.py
```

The research form accepts optional operator-entered evidence scores. Enter only scores backed by evidence you have verified; the UI labels these as operator input, not vendor data. The API can opt in to timestamped Yahoo Finance reported-EPS event evidence with `FUNDAMENTAL_PROVIDER=yahoo`; this does not supply sector direction or mappings. AI strategy rankings and model-reported confidence are advisory and are not calibrated forecasts.

The API's separate market-intelligence service exposes `POST /intelligence/opportunities` for timestamped multi-instrument proposals. It requires the configured AI provider, per-market `RESEARCH_SESSIONS`, `NEWS_PROVIDER=finnhub`, and a Finnhub API key. Results combine regime/news/strategy research and deterministic signal aggregation, returning explainable proposals marked `risk_status=NOT_EVALUATED` and `execution=NOT_SUBMITTED`; those in-memory objects are advisory and cannot be submitted. Only an approved proposal persisted by the Phase 2C-3 risk workflow can be accepted through `POST /intelligence/proposals/{proposal_id}/submit`. Automatic risk-based sizing is the default; manual quantity requires explicit `MANUAL_OVERRIDE`. Submission reloads linked run/signal/version provenance, requires fresh market data and a regular session, and routes through `TradingService`/`RiskEngine`/`OrderManager`.

Authenticated `POST /paper/cycles` manually runs a bounded PAPER-only research-to-order cycle with durable idempotency and per-candidate checkpoints; cycle status and explicit recovery are available through `/paper/cycles` routes and the `paper-cycle*` CLI commands. Paper executor orders/attempts and fill sequence are persisted for restart reconciliation (migrations V13/V14), and retry is authorized only when durable executor state proves the stable client order was not accepted. `POST /paper/positions/manage` evaluates fresh quotes against persisted entry stops/targets and routes exits through the same boundary. Cycles and exit monitoring are scheduled only by the opt-in in-process loop (`PAPER_AUTO_LOOP=true`, PAPER mode, single market, one API worker); otherwise they are manual. Automatic selection may choose only an `ACTIVE_FOR_PAPER` Strategy Lab version for the instrument that still passes `paper-review-v1` (preregistered holdout, cost-adjusted lower 95% expectancy bound > 0, drawdown <= 10%, explicit human approval) and whose holdout ended within the operator-set `AUTOMATIC_EVIDENCE_MAX_AGE_DAYS`; otherwise the cycle abstains with `NO_VALIDATED_STRATEGY`, records the per-strategy reason and places no orders. Unset expiry keeps every automatic entry blocked. `GET /paper/diagnostics` (also shown on the dashboard) reports the last cycle time, market, bar freshness, strategy eligibility, primary rejection reason and order/fill counts. None of this establishes unattended operation or production readiness. Both legacy Streamlit dashboard order mutators fail closed before changing their separate local paper state; see [docs/EXECUTION_BOUNDARY.md](docs/EXECUTION_BOUNDARY.md). Active-order reservation remains process-local, so only one API worker is supported.

Platform API setup, secret handling, migration, backup, restore, health checks, and current operational limitations are documented in [docs/OPERATIONS.md](docs/OPERATIONS.md).

### Windows: start the current US paper-trading dashboard

Use the API-backed [dashboard_app.py](dashboard_app.py), not the legacy
[dashboard.py](dashboard.py). The launch configuration is maintained in
[.vscode/tasks.json](.vscode/tasks.json).

1. Open this repository folder in VS Code. Run **Python: Select Interpreter**
   and select the Python environment where `requirements.txt` is installed
   (see Setup above). Use **File > Open Folder**, select the repository, and
   open **Terminal > New Terminal** with PowerShell. All commands below run
   from the repository root, not from `src` or `docs`:
   ```powershell
   Get-Location
   Test-Path -LiteralPath '.vscode\tasks.json'
   ```
   The last command must return `True`. On a new installation only, follow
   Setup to create `.venv` and install dependencies, then select that interpreter.
   Do not reinstall packages or overwrite `config.json` for routine startup.
2. Before starting, check whether the services already exist:
   ```powershell
   Get-NetTCPConnection -State Listen -LocalPort 8000,8501 -ErrorAction SilentlyContinue |
       Select-Object LocalPort,OwningProcess
   ```
   Reuse existing services; do not launch a second API worker. Do not stop an
   existing worker during a cycle merely to refresh the dashboard.
3. Verify the existing local secret files without printing their contents:
   ```powershell
   'secrets\local_dashboard.secret','secrets\twelvedata_api_key.txt',
   'secrets\ai_api_key.txt','secrets\finhub-api-key' |
       ForEach-Object { [pscustomobject]@{Path=$_; Exists=(Test-Path -LiteralPath $_)} }
   ```
   These supply API authentication, Twelve Data, OpenAI and Finnhub respectively.
   Obtain missing credentials through your approved secret-handling process;
   never commit them.
4. Confirm that the existing `outputs\paper_auto_us.db` is the intended US/USD
   paper account and has the required schema. The task sets `AUTO_MIGRATE=false`.
   If startup reports missing migrations, stop and follow the separately approved
   backup/migration procedure in [docs/OPERATIONS.md](docs/OPERATIONS.md).
   Do not delete, reset, fund or replace the database to fix startup.
   **Known source/database alignment requirement:** experiment preregistration
   adds V27; the Kite REST usage ledger adds V28. Migrations are needed only
   when code introduces schema changes, not for each restart. An API error
   containing `pending migrations [...]` means the backend
   has not started, even if Streamlit opens. Request approval for a WAL-safe
   backup, integrity/account-history verification and the supported migration
   procedure. Do not set `AUTO_MIGRATE=true` as a workaround.
5. For an explicitly authorized automatic US paper session, run **Tasks: Run
   Task** from the command palette and choose **Start US Paper Auto-Loop API**.
   Wait for Uvicorn's application-startup confirmation on port 8000.
   Use the VS Code task itself: copying only its command omits `options.env`,
   including research sessions and news settings.
   To inspect progress, use **Terminal > Run Task** or **Ctrl+Shift+P > Tasks:
   Show Running Tasks**, then select its terminal. A traceback or
   `terminated with exit code: 1` is a failed startup, not a running backend.
6. Run **Tasks: Run Task** again and choose **Start platform PAPER dashboard**.
   Open <http://127.0.0.1:8501/>. The API is <http://127.0.0.1:8000/>.
7. Verify the running configuration and gates:
   ```powershell
   $headers = @{
       Authorization = 'Bearer ' + (Get-Content -LiteralPath 'secrets\local_dashboard.secret' -Raw).Trim()
   }
   Invoke-RestMethod http://127.0.0.1:8000/health
   Invoke-RestMethod http://127.0.0.1:8000/paper/loop -Headers $headers |
       ConvertTo-Json -Depth 6
   Invoke-RestMethod http://127.0.0.1:8000/paper/diagnostics -Headers $headers |
       ConvertTo-Json -Depth 6
   Remove-Variable headers
   ```
   Confirm PAPER mode, US market, recovery completion, monitor health and entry
   controls. Review session/calendar, feed freshness, strategy and rejection
   diagnostics before expecting orders. `RUNNING` alone does not mean entries
   are permitted. Do not clear leases, bypass recovery or resume paused entries
   just to obtain an order.
8. Check the separate kill switch without resetting it:
   ```powershell
   $headers = @{
       Authorization = 'Bearer ' + (Get-Content -LiteralPath 'secrets\local_dashboard.secret' -Raw).Trim()
   }
   Invoke-RestMethod http://127.0.0.1:8000/kill-switch -Headers $headers |
       ConvertTo-Json -Depth 6
   Remove-Variable headers
   Invoke-RestMethod http://127.0.0.1:8501/_stcore/health
   ```
   Streamlit should return `ok`. The API must report PAPER/US, completed recovery
   and a healthy monitor; a restored kill switch or durable entry pause requires
   separate review/authorization, not an automatic reset.
9. In the browser, click **Refresh** and select the newest paper cycle in the
   sidebar. During an eligible US session the configured research cadence is
   10 minutes plus provider/AI processing time. Market closure or blocked gates
   can leave the last session's research visible; waiting cannot repair failed
   recovery or a kill switch. Review **Orders**, **Portfolio**, **Strategies**
   (completed paper outcomes) and **System health** rather than treating research
   rows as orders.
10. Keep both task terminals running while using the app. Refreshing or closing
    the browser does not stop the API. For an intentional shutdown, first review
    open positions/orders and arrange continued protection or approved closure;
    do not stop the sole protective worker with unmanaged positions. Use
    **Ctrl+C** in the API terminal and wait for shutdown to finish before starting
    another worker; stop the Streamlit terminal separately with **Ctrl+C**.
    Never delete a lock or database file to make a restart succeed.

Troubleshooting:

| Symptom | Action |
| --- | --- |
| Browser opens but API is unavailable | Inspect the API task terminal; dashboard health alone does not verify the backend. |
| `pending migrations [...]` | Stop startup attempts and obtain approval for backup/verification/migration; preserve existing cash and history. |
| `RESEARCH_SESSIONS` missing/invalid | Launch the complete VS Code task, including `options.env`, not a copied partial command. |
| `news_max_items must be an integer between 1 and 20` | Set `CANDIDATE_RESEARCH_SETTINGS.news_max_items` within 1–20; the US task uses five recent news items per candidate to fit its AI request budget. This is not the researched-stock count. |
| `AI_INVALID`: snapshot context exceeds the configured AI input limit | Review the reported character count and limit. Keep the US task's five-item news budget with `AI_MAX_INPUT_CHARS=16000`; oversized contexts remain rejected, not truncated. Restart requires separate controlled approval; do not activate the staged universe to fix this. |
| Python interpreter placeholder or module not found | Select the correct interpreter; verify dependencies in that environment. |
| Address/worker ownership already in use | Inspect the existing PID and reuse the service; do not start a second worker or kill an unknown process. |
| `PARTIAL`, `EXITS_ONLY`, active kill switch or stale feed | Read the exact diagnostics and resolve the cause; do not bypass the gate to obtain trades. |

The current US task opts into a 10-minute automatic cycle and
`PAPER_EXPLORATORY_AUTO_ENTRIES=true`: exploratory strategies are **not validated
performance certifications**. This task is not a dashboard-only/read-only launch.
Risk, quote, session, instrument and paper-execution gates still apply; starting
the task neither guarantees an order nor overrides persisted safety controls.
It uses OpenAI `gpt-4.1-mini`, not local Ollama. The earlier Ollama connectivity
test did not switch the application to `qwen3:8b`. India execution remains a
separate readiness task.

### US research universe and daily paper-trade policy

**Conservative existing-account restart:** while expansion capacity is unresolved,
use **Start existing US paper account (no universe import)**, not **Start US
Paper Auto-Loop API**. The conservative task opens the same account without
importing a master file, keeps the ten/day cap and five-position default,
and applies the five-news-item AI fix. It preserves whatever instruments are
already persisted; it does not remove instruments or reset cash/history.
The existing audited entry pause restores on startup; this task itself does
not create a pause on a fresh/unpaused account. Verify the pause before and
after maintenance, keep protective monitoring enabled, and never start it
while port 8000 or the account worker lease is owned.

Use Ctrl+C in the existing US API task terminal and wait for application
shutdown to finish before launching the conservative task. Review open
positions/pending orders and continued protection first. Do not terminate an
unknown process, force-stop protection, resume entries or use the staged task
as a shortcut. Provider capacity remains a separate prerequisite even for
the smaller account. A paused loop does not automatically research or open
new positions; historical analysis and explicitly budgeted advisory research
are learning workflows, not permission to trade.

The configured [US paper master](config/instruments_us_paper.json) contains
30 instruments (28 equities and two ETFs), retaining the original ten.
The loop's unset `PAPER_AUTO_LOOP_TOP_N` researches the eligible configured
universe, not a guaranteed number of signals or orders. The US task explicitly
sets `MAX_TRADES_PER_DAY=50`; the development default remains 10 for other
launches. The five-simultaneous-position limit and all other risk limits remain
unchanged. Strategy confirmation alone never authorizes an order.

The 20 added symbols are AVGO, NFLX, COST, ADBE, INTC, CSCO, JPM, BAC, GS, V,
MA, UNH, JNJ, ABBV, XOM, CVX, CAT, WMT, HD and PG. Their symbols, primary
listing venues, equity classifications and non-test status were checked against
Nasdaq Trader's [US symbol directory](https://www.nasdaqtrader.com/dynamic/symdir/nasdaqtraded.txt)
on October 30, 2026 (file stamp `1030202609:17`). `Q` maps to Nasdaq/XNAS
and `N` to NYSE/XNYS; notably WMT uses XNAS. This is listing verification,
not provider-entitlement, liquidity, current tick-size or strategy certification.
The additions use the existing paper master conventions of whole-share lots,
USD 0.01 ticks and no shorting. Fresh provider data, liquidity filters and
per-order validation remain required; no stale data or assumed fills are added.

**Activation pending capacity review:** these file changes do not reconfigure a
running worker. The task defaults to eight Twelve Data requests/minute. Five
open positions monitored every 30 seconds can require ten quote requests/minute
before research, pending-order or entry traffic. The separate quote resilience
lane still shares the underlying provider's quota. A full 30-member scan plus
candidate-bar research can additionally require 60 bar requests per cycle
before strategy/news/AI and risk evaluation. Neither expanded coverage nor
50 daily trades is certified at the default provider budget. Verify the actual
provider plan, total shared-key usage (including India/Market Watch), daily
credits, cycle latency and protective-monitor capacity before an authorized
controlled restart. Do not raise a quota speculatively, slow protection or
remove entry gates to activate the expansion. No schema migration is needed
for this configuration change.

#### Capacity planning for the staged US expansion

Planning assumptions: a normal 390-minute US session, 39 ten-minute cycles,
all 30 instruments eligible, one scanner bar read AND one PAPER scanner quote,
one candidate-research bar read and one deterministic-signal bar read per
instrument per cycle. This corrects the earlier estimate that omitted PAPER
scanner quotes. Five
positions held throughout the session require up to 3,900 scheduled monitoring
quotes at the unchanged 30-second cadence. The adapter uses Twelve Data
`time_series` for both bars and quotes; its published weight is one credit per
symbol for this endpoint.

| Workload | Illustrative credits/session |
| --- | ---: |
| Scanner bars: 30 x 39 | 1,170 |
| PAPER scanner quotes: 30 x 39 | 1,170 |
| Candidate research bars: 30 x 39 | 1,170 |
| Deterministic signal bars: 30 x 39 | 1,170 |
| Protective quotes: 5 x 2/minute x 390 | 3,900 |
| Cached daily risk-stat bars: 30 symbols | 30 |
| Entry quote allowance: illustrative two reads x 50 entries | 100 |
| Planning baseline | 8,710 |
| Baseline plus 30% headroom | 11,323 |

Use **at least 11,323 credits per US session, preferably no daily cap**, as an
initial budget target, not a proven upper bound. Rejected proposals, repeated
final validation, pending orders, exits, retries, restarts, manual research,
Market Watch and shared India usage can add requests. The 50-trade cap does not
cap rejected attempts or provider traffic. News/Finnhub and AI have separate
quotas and costs.

The baseline averages about 22.3 credits/minute, but research runs in bursts.
The earlier procurement scenario targeted **144 credits/minute or more** with verified
real-time US equity **and ETF** entitlement and permitted application usage.
This is a procurement/planning target, not a measured safe configuration:
even 144/minute can be exhausted by clustered research and risk validation.
The new US capacity layer reserves credits for existing positions/pending
orders before admitting research and shares one REST cache/ledger with Market
Watch. It forecasts sustained load plus monitoring/entry burst headroom; that
forecast is not a measured latency guarantee. Confirm headroom under full-universe
research plus five-position monitoring before activation; do not simply raise
`TWELVEDATA_REQUESTS_PER_MINUTE` or infer safety from the average.

Checked October 30, 2026: Twelve Data's
[published pricing](https://twelvedata.com/pricing.md) lists Basic at 8/minute
and 800/day, and Grow configurations at 55, 144 and 377/minute without a daily
cap. These public offerings do not establish the operator account's allowance,
ETF entitlement or licensing; confirm those directly with the provider.
Neither the reported 100/day allowance nor published Basic capacity meets
this workload. The expansion remains staged, with no quota changes or restart.

Subsequent direct verification on October 30 used the configured provider's
`api_usage` endpoint: it reported Basic, eight credits/minute and 800/day,
correcting the earlier unverified 100/day report. A bounded AAPL adapter read
also passed the existing 300-second freshness and tick checks (about 126
seconds old). This proves neither continuous feed availability nor capacity
for research plus protection. Health reads do not refresh quotes; a flat,
entry-paused worker can report no execution data after restart without the
provider itself being unavailable. Entries remain paused; no allowance was
increased and no timestamps were rewritten.

The [US monitoring-capacity procedure](docs/OPERATIONS.md#us-twelve-data-monitoring-capacity)
documents the new authenticated `/paper/monitoring-capacity` status, local
credit ledger, evidence requirements and failure handling. `/health` and the
dashboard Health tab also expose the gate and per-position source ages.
Default client limits are not entitlement evidence. Unconfirmed allocation
blocks nonessential US reads and automatic entries, while existing-position
monitoring still attempts bounded, validated REST reads. Neither a checkbox,
two observed updates nor a healthy flat-account monitor can override an
insufficient quota forecast. No supported WebSocket transport or account
entitlement was found; `websocket` configuration reports unavailable and blocks
entries rather than silently pretending to stream. These changes have only
static validation, have not been loaded into the running worker, and do not
activate the staged universe or clear the operator pause.

## Launch legacy research dashboard
The dashboard can still display:
- Top 5 intraday candidates scanner
- Buy strategy guidance with signal/risk levels

**Legacy order execution is disabled.** The former Buy/Exit paths fail closed
and do not change the dashboard's local portfolio or ledger. Use the
authenticated platform PAPER API for risk-checked orders. Existing dashboard
state files are left untouched and are not imported into the platform portfolio.

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run dashboard.py
```

## Launch simple legacy research page (fast page)
This page is optimized for quick load and minimal controls:
- Budget-first setup (default `Rs 200000`)
- Top 5 buy/sell signals only
- Legacy simulator order mutations are disabled
- Daily target progress tracking (default `Rs 4000`)

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run dashboard_simple.py --server.port 8507
```

## Legacy Dashboard Research Behavior
The advanced dashboard in [dashboard.py](dashboard.py) retains scanner and
signal-display behavior. Its former local paper-execution helper has been
removed; remaining legacy order controls fail closed. Use the platform API for
paper execution.

1. Market window logic
- Market open: 09:15 IST
- Entry cutoff: from `config.json` (`entry_cutoff_time`, default 13:30 IST)
- Square-off: from `config.json` (`square_off_time`, default 15:15 IST)
- All time calculations use `market_timezone` from `config.json`.

2. Top 5 candidate ranking
- Universe: internal watchlist of liquid NSE symbols.
- Ranking factors: strategy score, intraday range percent, momentum context.
- Data source options:
  - NSE quote mode (faster, avoids Yahoo limits)
  - Yahoo OHLC mode (bar-based signals)

3. Entry strategy modes
- `ORB + VWAP`: breakout above OR high with VWAP support.
- `VWAP Trend`: continuation above VWAP with momentum/volume confirmation.
- `OR Reversal`: reversal near OR low with recovery confirmation.

4. Buy/Exit button logic
- `Buy Now` is disabled until strategy entry trigger is reached.
- `Buy Now` is also disabled when a position already exists for that symbol.
- `Exit` is enabled only when one of these is true:
  - stop-loss reached
  - target reached
  - square-off time reached

5. Tracking and PnL
- Strategy tracking list stores expected entry, trigger state, SL/TP, and benefit %.
- Dummy portfolio tracks cash, holdings, average price, unrealized PnL, and trade log.

6. Top 5 after time-up
- After entry cutoff time, Top 5 scanner and recommendations are hidden automatically.
- This is intentional to prevent new intraday entries outside planned window.

## Dashboard Usage (Recommended Flow)
1. Start app:
```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m streamlit run dashboard.py --server.port 8505
```
2. Choose data source and strategy mode in sidebar.
3. During entry window, review Top 5 list and trigger states.
4. Treat `READY` as research output only; it does not authorize an order.
5. Do not use legacy Buy/Exit controls; they fail closed.
6. Review historical dashboard state only as a separate legacy record.

## Historical Legacy Dashboard Notes
The following notes describe old dashboard behavior and are not permission to
restore dashboard-local order execution. Any future order path must use the
platform PAPER service; the execution boundary is documented in
[docs/EXECUTION_BOUNDARY.md](docs/EXECUTION_BOUNDARY.md).

1. Data and refresh behavior
- Primary live mode is NSE quote mode (`NSE Quote API (non-Yahoo)`).
- Auto refresh is enabled from sidebar and defaults to 30 seconds.
- Refresh should update Top candidates table prices and trigger fields, not only reload page chrome.
- Top scan can re-run during refresh cycles in NSE mode.

2. Trading windows and safety gates
- No entries before OR end (observation-only during opening range formation).
- New entries only inside entry window (`OR_END` to `entry_cutoff_time`).
- Exit logic remains active for SL/TP and square-off windows.
- Top candidates are hidden after cutoff to prevent fresh entries.

3. Position and notional risk caps (hard constraints)
- Per-symbol quantity cap: `MAX_POSITION_QTY_PER_STOCK = 200`.
- Per-symbol notional cap: `MAX_BUY_NOTIONAL_PER_STOCK = 50000`.
- Both caps must be enforced for new buys and existing positions.
- Existing oversized positions are auto-adjusted and logged in trade history.

4. Auto-buy sizing
- Auto-buy can run fixed quantity or balance-spread mode.
- Balance-spread mode allocates by available cash and remaining slots, then clamps by risk caps.
- Estimated margin + charges must be affordable before order execution.

5. Buy quality gate (required before auto-buy)
- Candidate must pass all quality filters:
  - minimum candidate score
  - minimum reward:risk
  - minimum expected edge after costs (round-trip estimate)
- If any check fails, buy is skipped with explicit reason in engine actions.

6. Learning-aware ranking and suggestions
- Scanner scores are adjusted with a small learning bonus from closed-trade history.
- Learning profile is symbol-level and strategy-aware when enough data exists.
- UI includes "Learning Agent Suggestions" for additional historically favorable symbols.

7. Self-tuning mode
- Optional auto-tune uses recent daily outcomes (last 5 days) to adjust quality filters.
- Weak recent performance should tighten filters; strong consistent performance may relax slightly.
- With no recent closed-trade days, system falls back to base thresholds.

8. Logging and explainability
- Every order/adjustment writes to in-memory log and persistent history file.
- Trade history is used for learning summaries, strategy comparison, and daily PnL export.
- AI changes must preserve human-readable action reasons in UI and logs.

9. Non-functional expectations for AI changes
- Prefer minimal, targeted edits.
- Keep paper-trading-only semantics.
- Do not remove or weaken hard risk caps.
- Any new buy logic must remain cost-aware (charges and margin).

Optional symbol override:
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli backtest --config config.json --symbol TCS.NS
```

## Check latest signals (no order execution)
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli signals --config config.json
```

## Run parameter sweep (fast tuning)
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli sweep --config config.json
```

Custom sweep values example:
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli sweep --config config.json --opening-ranges 10,15,20 --stop-losses 0.003,0.004 --take-profits 0.006,0.008,0.01 --volume-spikes 1.1,1.2 --top 10
```

The sweep command saves a ranked CSV in `outputs/`.

## Replay top sweep result
Run a full backtest using the best ranked sweep row (rank 1 by default):
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli replay-best --config config.json
```

Replay a specific rank from a specific sweep file:
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli replay-best --config config.json --sweep-file outputs/sweep_RELIANCE_NS_20260417_120000.csv --rank 3
```

Replay with quality filters (minimum trades and drawdown cap):
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli replay-best --config config.json --min-trades 25 --max-drawdown-pct 0.08 --rank 1
```

This command exports:
- `replay_trades_*.csv` (detailed trades)
- `replay_config_*.json` (exact config used)

## Walk-Forward Validation and Robustness

Run offline chronological train/test validation for the configured symbol:
```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m stockmarket.cli validate --config config.json --period 60d
```

Use rolling or expanding training windows, an optional session gap, and explicit parameter grids:
```powershell
.\.venv\Scripts\python.exe -m stockmarket.cli validate --config config.json --period 60d --window-mode rolling --train-sessions 20 --test-sessions 5 --step-sessions 5 --gap-sessions 1 --min-train-trades 5
```

For each fold, parameters are selected from training sessions only and then frozen for the subsequent out-of-sample sessions. OOS windows do not overlap. The command also runs one-factor robustness scenarios for opening range, stop loss, target, volume settings, VWAP price source, commission, and slippage. `typical` remains the default VWAP source; `close` is an experimental sensitivity case and is not selected into dashboard settings.

Validation writes three files under `outputs/` (or `--output-dir`):
- `validation_<symbol>_<run>.json`: versioned machine-readable report.
- `validation_<symbol>_<run>.csv`: fold/scenario rows with separately prefixed train and OOS metrics.
- `validation_<symbol>_<run>.md`: human-readable report.

Reports include total return, win rate, profit factor, expectancy, maximum drawdown, daily-equity Sharpe and Sortino, average win/loss, consecutive wins/losses, trade count, exposure, and fold-level/aggregate OOS results. Undefined metrics are represented as `null` in JSON rather than `Infinity` or `NaN`.

The validation backtest uses a signal-at-close, fill-at-next-observed-bar-open model with adverse percentage slippage. Stop/target checks begin after that fill; if both are crossed within a later OHLC bar, stop is assumed first. Input bars with invalid timestamps or OHLCV values are rejected. Missing bars are not synthesized. Yahoo Finance data and these simulated fills are research assumptions, not guarantees of executable or profitable results. Existing dashboard learning/self-tuning and paper execution are unchanged and are not part of this validation command.

## Notes
- Yahoo intraday history has period/interval limits. If data is missing, reduce `period` or change interval.
- Yahoo can temporarily rate-limit requests. If this happens, wait and retry, reduce request frequency, or use a different symbol/time window.
- This is a research and education tool, not investment advice.
- Start with paper testing before any real trading.
