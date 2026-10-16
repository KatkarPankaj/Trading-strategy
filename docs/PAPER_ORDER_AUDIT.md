# Paper-order audit and India readiness plan

Date: 2026-10-15. Baseline: `310b406`. Scope: local source, existing persisted
decisions, read-only loop/health/recovery endpoints, and isolated offline tests.
This is not deployment, exchange-feed certification or a profitability claim.

## 1. Prioritized root-cause report

| Priority | Finding and evidence | Disposition |
| --- | --- | --- |
| P0: expected abstention, not an execution defect | Operator history contains 26 generated signals, all HOLD/confidence 0. Twenty most recent generated records have NO_TRADE; reasons across all 26: 22 NO_CONFIRMED_BREAKOUT, 4 PRICE_OFF_TICK. There are zero orders, fills and positions. | Do not force entries, round off-tick prices to manufacture signals, or weaken breakout/volume filters. |
| P1: confirmed strategy-selection contract defect | Persisted assessments contain `STRATEGY_SELECTION_REJECTED: unknown strategy candidate: orb_vwap v1.0.0`. The frozen catalog has ID `orb_vwap`, implementation `OrbVwapStrategy`, version `1.0.0`, separately. The response schema previously permitted any string. | Bound the model-facing strategy field to the exact requested ID enum and explicitly prohibit version/display-label concatenation. Continue rejecting invented IDs; do not strip suffixes or substitute versions. Downstream version/configuration checks remain. |
| P1: confirmed lost rejection explanation | Six of the latest 20 cycle candidates were RESEARCH_REJECTED with null cycle-level error, although the linked research assessments contained precise failures. | Carry assessment failures into candidate checkpoints and persist a structured decision trace. |
| P1: confirmed replay false-positive class | The prior connection-local TEMP counter correctly excludes same-connection non-history writes, but `PRAGMA data_version` changes on *any* other-connection commit. A disposable two-connection non-history commit reproduced `HISTORY_SNAPSHOT_CHANGED: fill replay is incomplete`. | Prepared migration 26 installs transactional persistent history-only counters/triggers. Real history mutations, missing pages, ordinal gaps, missing counters/triggers and interrupted recovery remain fail-closed. |
| P2: request-envelope budget gap | Character admission examined research context, not the full HTTP body, strict schema duplication or completion reserve. An observed context is 7,715 characters but the full prepared assessment request is 18,229 bytes. | Enforce full serialized envelope plus explicit completion reserve before HTTP. Reject rather than truncate oversized input. |
| P2: orchestration status ambiguity | SUBMITTED orders were classified as ORDER_REJECTED by the cycle's terminal-state set. A fixture demonstrates the incorrect classification. | Recognize durable submission without claiming a fill. Preserve the actual broker/order status and quantity. |
| P2: cadence/quota qualification remains incomplete | An isolated narrow breakout is caught by one 10-minute scheduling phase and missed by the other. Local quote-lane capacity is not an account-credit reservation. | Do not change cadence on this evidence alone; require timestamped replay and quota measurements. |

No matching signal-export file was attached or found locally. The supplied
20-row description is tested as a synthetic display fixture, and independently
matches the latest 20 persisted generated signals. They are abstentions, not
actionable BUY/SHORT records or orders. The dashboard heading/caption now makes
this explicit; the separate actual paper-order/fill ledger is retained.
The matching generated records have intent NO_TRADE, side HOLD, confidence 0
and regime UNKNOWN; they are a different sample from the cycle candidates below.

## 2. Actual path and one traced candidate

The owning path is:

1. Scanner produces bounded candidates; `AutonomousResearchService` persists
   snapshots, assessment and the frozen strategy catalog.
2. AI assessment/selection is advisory. Only STRATEGY_SELECTED candidates enter
   `SignalGenerationService`, which checks paper scope, identity, opportunity
   age, registration/version/configuration, instrument/session and completed,
   valid timestamped bars before calling the deterministic strategy.
3. `TradeProposalService` loads the persisted signal. HOLD/NO_TRADE exits as
   NON_ACTIONABLE before creating a proposal. Executable signals still require
   market/currency/account context, position sizing and RiskEngine approval.
4. `PaperProposalExecutionService` revalidates approval and strategy provenance,
   age, session, existing positions/orders and a fresh execution quote. It
   claims a deterministic proposal/order identity, never an arbitrary AI order.
5. `TradingService` prepares current quotes/statistics and revalidates changed
   state, final gate/admission, sizing, fees/slippage, buying power and RiskEngine
   under the existing execution/reservation boundary.
6. `OrderManager` persists lifecycle transitions before simulated broker work.
   Orders, events, paper execution, fills and portfolio state use existing
   transactional persistence. Recovery reconstructs history and reconciles;
   partial or inconsistent replay cannot authorize entries.

Sources: [cycle](../src/stockmarket/core/autonomous_paper_trading.py),
[research](../src/stockmarket/core/autonomous_research.py),
[signals](../src/stockmarket/core/signal_generation.py),
[proposals](../src/stockmarket/core/trade_proposals.py),
[paper lifecycle](../src/stockmarket/core/paper_lifecycle.py),
[final trading boundary](../src/stockmarket/core/trading_service.py),
[risk](../src/stockmarket/core/risk.py),
[order management](../src/stockmarket/core/order_management.py),
[recovery](../src/stockmarket/core/recovery.py).

Observed cycle `22d334a285b25b5887645d2010db1f29`, research run
`4f6e156fb77158308ab5fe426b343f1f`, candidate `ARCX:SPY`:
research time `2026-10-14T19:00:00.758006+00:00`; assessment COMPLETE;
registered `orb_vwap` / `1.0.0`; deterministic result HOLD / NO_TRADE /
confidence 0 / `NO_CONFIRMED_BREAKOUT`; proposal result
`NO_SIGNAL: HOLD/NO_TRADE signals cannot create a TradeProposal.` There was no
approved entry proposal, execution-quote admission, submission or fill. The
research record correctly did not become an order.

## 3. Exact sampled non-order reasons

Latest 20 persisted cycle-candidate rows, descending insertion order.
Cycle prefixes below are unique in this sample. This table joins the linked
assessment for legacy rows whose cycle error was discarded; it does not rewrite
any operator record.

Reason keys:

- B: `NO_CONFIRMED_BREAKOUT`; deterministic NO_TRADE.
- T: `PRICE_OFF_TICK`; deterministic NO_TRADE.
- S: `STRATEGY_SELECTION_REJECTED: unknown strategy candidate: orb_vwap v1.0.0`;
  assessment NO_VALID_STRATEGY; signal never generated.
- V: `SCHEMA_VALIDATION_FAILED: ValidationError [<root>:value_error]`;
  assessment AI_INVALID; signal never generated. The old safe diagnostic did not
  retain the validator's specific root message or raw model response, so a more
  specific historical diagnosis is unavailable; no field/value is invented.

| # | Cycle prefix | Candidate | Persisted status | Exact reason key |
| --- | --- | --- | --- | --- |
| 1 | 22d334a285 | XNAS:QQQ | NON_ACTIONABLE | B |
| 2 | 22d334a285 | ARCX:SPY | NON_ACTIONABLE | B |
| 3 | 28b753353f | XNAS:QQQ | NON_ACTIONABLE | B |
| 4 | 28b753353f | ARCX:SPY | NON_ACTIONABLE | B |
| 5 | f5bddb0ffa | ARCX:SPY | NON_ACTIONABLE | B |
| 6 | f5bddb0ffa | XNAS:QQQ | RESEARCH_REJECTED | V |
| 7 | 92df4fe14d | XNAS:QQQ | NON_ACTIONABLE | B |
| 8 | 92df4fe14d | ARCX:SPY | RESEARCH_REJECTED | S |
| 9 | 1c51c3ec01 | XNAS:QQQ | NON_ACTIONABLE | B |
| 10 | 1c51c3ec01 | ARCX:SPY | NON_ACTIONABLE | B |
| 11 | 26790a8919 | XNAS:QQQ | NON_ACTIONABLE | B |
| 12 | 26790a8919 | ARCX:SPY | NON_ACTIONABLE | T |
| 13 | d1a6096877 | ARCX:SPY | NON_ACTIONABLE | B |
| 14 | d1a6096877 | XNAS:QQQ | RESEARCH_REJECTED | S |
| 15 | df04243ca6 | XNAS:QQQ | RESEARCH_REJECTED | S |
| 16 | df04243ca6 | ARCX:SPY | RESEARCH_REJECTED | S |
| 17 | 5c87e314fc | XNAS:QQQ | NON_ACTIONABLE | B |
| 18 | 5c87e314fc | ARCX:SPY | NON_ACTIONABLE | B |
| 19 | b5a8e90ea0 | ARCX:SPY | NON_ACTIONABLE | B |
| 20 | b5a8e90ea0 | XNAS:QQQ | RESEARCH_REJECTED | V |

Every B/T row also retains the proposal-layer NO_SIGNAL reason above. The sample
has 13 breakout abstentions, one off-tick abstention, four exact-ID selection
failures and two invalid-schema failures.

## 4. Running-service and gate evidence

Read-only process inspection: API PID 32680 started 02:32:44 IST; dashboard PID
14348 started 02:22:46 IST on October 15. Terminal output contains matching
server-start/application-startup messages. Source baseline HEAD was committed
08:41:55 IST; no runtime build hash exists to certify every loaded module against
that commit. The newly prepared changes are not loaded into that API.

Operator schema is 25. It was current at the start of this audit; the prepared
code now requires 26. **No migration or restart was performed.** A new startup
with migration disabled will refuse the old schema. Do not attach a second
worker, bypass this mismatch, or implicitly migrate the operator file.

At the inspected API snapshot:

| Gate | Observed state / limitation |
| --- | --- |
| Paper restriction | PAPER application/trading mode; no live execution authorized. |
| Durable entry pause | Entries enabled; latest audit control is PAPER_LOOP_ENTRIES_RESUMED at 2026-10-14T15:29:00.600468+00:00. Earlier pause is preserved. |
| Kill switch / recovery | Gate open; `/recovery` reports halted false, no reasons. Loop reports no recovery failures; protective recovery complete. These are runtime reports, not independent broker certification. |
| Market/session | US CLOSED. Health blockers: NO_REGULAR_MARKET_SESSION and MISSING_OR_STALE_EXECUTION_DATA. |
| Quotes | No market-data timestamp has been received by the currently inspected process. A healthy circuit is not proof of a fresh execution quote. |
| Monitor | Independently enabled, HEALTHY, 30-second interval, entry_ready true, no positions or pending orders checked. Empty-book health does not demonstrate protective fills on a real feed. |
| Signal/version | All historical generated signals were non-executable; four sampled assessment selection failures used a display label instead of exact ID. |
| Currency/cash | US worker is configured USD; observed cash/equity USD 100,000, gross exposure 0 and fees 0. No INR funding or implicit FX conversion is authorized. No positions/orders/fills were observed. No balance was changed or account reset. |
| Duplicates/risk/costs | No disabled risk controls reported; configured risk/trade 0.5%, daily loss 2%, drawdown 10%, maximum positions 5, trades/day 10 and slippage 20 bps. Existing deterministic order keys, open-position/open-order checks, reservations, fees, risk and sizing remain; exercised in isolated tests. No observed candidate reached these later gates with an executable signal. |
| Provider/rate limits | Research/quote circuits reported closed, but no fresh quote was observed. Live vendor entitlement, remaining credits and session-time latency were not measured. |

The previously documented HISTORY_SNAPSHOT_CHANGED incident is not present in
the currently queried cycle payloads and was not reproduced by changing the
operator file. The disposable reproduction proves a false-positive class, not
that every historical snapshot rejection was spurious.

## 5. Structured decision contract

New autonomous-cycle checkpoints persist `decision_trace` inside existing
candidate JSON; no new order route or decision-table abstraction was added.
It contains candidate/cycle IDs, canonical instrument and symbol,
strategy/version (null when selection never succeeded), aware decision timestamp,
signal outcome, ordered gate outcomes and provenance, final state, rejection code,
human-readable reason and actual client-order ID only when an order exists.
Final execution traces retain quote source/timestamp and final risk decision.

The four gate groups are research eligibility; strategy registration/signal;
proposal risk/sizing; fresh quote/session/final execution risk. Unevaluated
groups explicitly say NOT_EVALUATED: they are not fabricated individual passes.
Final states distinguish NO_TRADE, REJECTED, BLOCKED, PENDING, SUBMITTED and
FILLED. PARTIALLY_FILLED retains its actual order state and is not labelled fully
FILLED. Cycle COMPLETE still means processing completed, not a trade or profit.
Legacy rows are marked LEGACY_TRACE_UNAVAILABLE rather than backfilled.

An independently blocked loop may have no discovered candidates at all; its
cycle/loop rejection reason remains the authoritative status. No candidate or
signal is invented to make an empty cycle resemble a decision/order.

## 6. Provider, quota, cadence and model audit

| Data surface | Configured/current implementation | Qualification |
| --- | --- | --- |
| Scanner/research bars and historical candles | Twelve Data OHLCV/time_series in the US task; Yahoo and mock alternatives exist. | Research-only; Twelve Data history capped at 5,000 bars per request. No certified India execution feed. |
| Execution/protective quote checks | Same configured provider, isolated quote lane; Twelve Data quote is latest 1-minute bar. | Not an exchange streaming tick feed. Source timestamps and freshness checks still apply. |
| News | Finnhub REST company news in US task. | Separate credentials; non-US symbols require explicit mapping; 429 propagates. No integrated India news entitlement assumed. |
| AI | OpenAI-compatible advisory assessment/selection; task model gpt-4.1-mini. | Never an order producer or risk approval; no paid model calls were made for this audit. |

Offline one-instrument production-composition cycle: **3 quote and 3 bar adapter
calls**. These are mock adapter calls, not HTTP attempts or billed credits.
Operations' roughly 16-credit/10-symbol scan, 8 requests/minute, 800 credits/day
figures are estimates/plan assumptions, not measured remaining account quota.
The 10-minute ~625-credit/day estimate leaves limited margin for retries,
execution quotes, Market Watch and protective monitoring; obtain provider
dashboard/request-ID measurements before relying on it.

The quote lane has a separate four-worker/semaphore capacity; automatic
protective calls use bounded timeout, zero rate-limit wait and one attempt.
That does **not** reserve vendor credits: the underlying limiter/account quota
is shared. Research retry can amplify transient failures. Existing adapters are
REST polling, not streaming; streaming reconnect/subscription replay is therefore
not implemented or certified. Tests inject timeouts/429/circuit failure; actual
feed recovery and account quotas remain unmeasured.

Deterministic replay uses the same completed 5-minute bars with a narrow 09:50
breakout; as_of is bar start +5 minutes and incomplete/future bars are excluded:

| Scan cadence/phase (bar-start grid) | Evaluations | BUY events |
| --- | --- | --- |
| 5m, +0 | 13 | one, 09:50 |
| 10m, +0 | 7 | same one |
| 10m, +5m | 6 | zero |
| 15m, +0 | 5 | zero |

The actual scan phase, research duration and completed-bar boundary matter:
10-minute cadence can miss an eligible transient setup, but does not miss every
setup. This fixture does not measure profitability, real-market signal count or
execution quality. Cadence is not bar interval; retain supported 5-minute bars.
Keep independently scheduled protection and budget both local and provider
capacity before considering a cadence change.

Offline serialization of one persisted assessment context:
7,715 context characters; 5,279 system characters; 7,740 wrapped-user characters;
18,229 wire bytes; explicit 4,096 output tokens; conservative admission upper
bound 22,389 including 64 framing tokens. No HTTP/model call was made.
The offline stub deliberately returned `{}`, which failed local schema
validation; this measurement is request size, not a successful AI decision.

The adapter now checks exact wire serialization, including both inline and strict
response-format schemas, wrappers and output budget. Byte count is a conservative
token upper bound, **not** an exact tokenizer count or vendor-reported usage.
The default 32,768 application cap is not autodetected model capacity. Match it
to independently verified model limits. Cumulative token usage remains
process-local, not a durable account spend limit or synchronized cross-thread/
retry reservation; provider-side billing limits are still required.

## 7. India observation-first readiness plan

Current status: **BLOCKED, not certified for orders**.

Reuse Market Watch, `MarketDataProvider`, canonical instrument persistence,
reviewed-calendar contracts, currency/account binding, RiskEngine, worker lease,
recovery and OrderManager. Do not add direct vendor/broker calls to strategies or
the dashboard.

### Required feed and identity evidence

- Evaluate a **data-only** Kite Connect adapter as a separate approved phase.
  No Kite adapter is registered today; an API subscription is not integration.
  Require a broker-authorized account/app, API credentials and current access
  token, licensed real-time NSE/BSE market-data/historical entitlements and
  permitted use. Store keys/tokens only in approved secrets, never in the DB.
- Bind canonical `XNSE:SYMBOL` / `XBOM:SYMBOL` to exchange, trading symbol and
  provider instrument token using a dated, reviewed instrument master.
  Validate ISIN where supplied, venue/series, tick, lot, currency, active status,
  corporate actions, delisting and token reuse; ambiguous mappings block entries.
- Test missing/extra instruments, token/symbol swaps, different venues with the
  same symbol, invalid tick/lot, stale master and corporate-action transitions
  with synthetic fixtures; separately review actual masters before promotion.
- Test source timestamp versus local receive time, aware Asia/Kolkata/UTC
  conversion, gaps, out-of-order/duplicate ticks, future timestamps, stale
  snapshots and completed historical candles. Define history retention and
  interval coverage from actual entitlement, not research-provider assumptions.
- For streaming, test disconnect, expired token, resubscription, sequence/gap
  recovery and restoration of a fresh snapshot before entries resume.
  A reconnect alone is not data readiness.
- Derive endpoint/account quotas from current broker documentation and actual
  subscription. TokenException/403 requires renewed authentication; 429 is not
  a usable quote. Reserve headroom for protection and hard-block when it is
  unavailable. This paper platform must never call the vendor's order API.

Official evaluation references:
[Kite instruments and quotes](https://kite.trade/docs/connect/v3/market-quotes/),
[historical candles](https://kite.trade/docs/connect/v3/historical/),
[WebSocket streaming](https://kite.trade/docs/connect/v3/websocket/),
[authentication](https://kite.trade/docs/connect/v3/user/),
[exceptions/rate-limit failures](https://kite.trade/docs/connect/v3/exceptions/).
Public documentation describes APIs, not this operator's purchased entitlements.
No paid credentials/subscriptions were verified or acquired.

### Calendar, funding, costs and worker evidence

- Obtain independently reviewed **XNSE and XBOM** publications covering the
  intended dates, including explicitly documented special sessions and
  corrections. Archive source bytes, publication locator, checksums, review
  boundaries and per-date evidence using the existing calendar contract.
  No verified India calendars are bundled. No holiday or Muhurat date is invented.
- Use a new, explicitly approved INR-funded paper account/database and a single
  IN execution market. Persist market/currency/seed identity. Do not reuse or
  re-seed the US database, convert USD implicitly, or use static FX funding.
- Review India-specific commission, applicable charges/taxes, spread/slippage,
  lot/tick rounding, exposure and available INR buying power for long and short
  scenarios. Validate fees and rejected quantities deterministically.
- Short eligibility must be explicit per instrument/product/session; no
  unrestricted overnight short capability is assumed. Paper-only risk and
  session/flattening constraints remain.
- Test worker ownership, conflicting market/currency restart, order/fill replay,
  genuine concurrent history changes, partial/corrupt state, reconciliation,
  durable pause/kill-switch restore and stop/target/EOD exits on a fresh feed.
  One execution market per worker is preserved; simultaneous US/India automation
  remains NOT_CERTIFIED.
- Existing readiness explicitly reports mapping/feed validation as false.
  Do not flip these flags, promote a research provider or enable India entries
  without separately approved evidence-backed integration and tests.

### Paper-only acceptance checklist (in order)

1. Review these uncommitted source changes and test evidence; obtain explicit
   approval for any later operational maintenance.
2. For US alignment only after approval: confirm paper mode and worker ownership,
   preserve durable pause/kill switch, back up and integrity-check the DB, apply
   migration 26, perform controlled shutdown/restart and verify exact code/schema.
   Compare cash, positions, orders, fills and audit integrity with the backup.
3. Run the isolated BUY and SHORT end-to-end tests. Confirm one deterministic
   order/fill, trace IDs, approved risk provenance and idempotent retry/recovery.
   Do not place a demonstration order in operator state.
4. During a separately authorized market session, observe provider timestamps,
   mappings, quote quality, historical bars, 429/timeouts, retries and actual
   account credits. Keep entries paused while collecting readiness evidence;
   never freshen stale prices by replacing timestamps.
5. Measure scan phase/cadence on one captured chronological dataset, preserving
   embargo/holdout isolation; qualify protection headroom under worst-case retries.
6. For India, complete the data-only adapter, canonical master and independent
   calendar review; prove entitlements and all negative-case mapping/feed tests.
7. Establish an explicitly approved separate INR paper account and reviewed
   costs/risk/short/session policy. Confirm clean recovery and lifetime ownership.
8. Test observation, pending-order cancellation, stop/target/EOD flattening,
   provider outages and durable entry pause with injected fixtures first.
9. Require every readiness gate to pass without bypass; only then separately
   authorize a supervised India PAPER trial. Rejections must remain visible.
10. Keep live mode disabled. No deployment, live order, account balance change,
    operator-state alteration, commit or push is part of this audit.

## 8. Validation

The editor test tool reported no discovered tests, so the repository's existing
unittest runner was used with Python 3.11.6.

- Red/green reproduction: two new tests initially failed (external non-history
  replay invalidation; absent exact strategy enum), then passed after source fixes.
- Final focused order/AI/history/dashboard/cadence regression: 106 tests passed
  in 13.282s, including independent protective exits during failed entry research.
- Provider/India/monitor tests: delegated offline run reported 119 passing tests
  plus 3 mocked Finnhub cases. No paid/network feed measurement is implied.
- Cadence/counting: two persistent offline regression tests passed. One test
  counts the six mock adapter calls described above.
- Final full suite: 727 tests ran in 207.480s, OK, with five PostgreSQL skips
  (722 passed, five skipped). Expected injected-failure logs and existing AnyIO
  warnings are not test failures.
- `git diff --check` passed; editor diagnostics for the changed source and final
  protective-exit test reported no errors. This is not a repository-wide static
  type-check, security scan, broker-feed certification or PostgreSQL test result.

Executed commands from the repository root (PowerShell, configured Python):

```powershell
$env:PYTHONPATH='src;tests'
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest test_protective_exit_isolation test_ai_strategy_selection test_ai_research_schema test_openai_structured_output test_autonomous_paper_trading test_paper_end_to_end test_dashboard_paper_activity test_paper_cadence_audit -q
$env:PYTHONPATH='src'
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest discover -s tests -q
git diff --check
```

These interpreter paths identify the execution environment only; they are not
application configuration or a portable deployment command.

Review [test_paper_end_to_end.py](../tests/test_paper_end_to_end.py),
[test_autonomous_paper_trading.py](../tests/test_autonomous_paper_trading.py),
[test_protective_exit_isolation.py](../tests/test_protective_exit_isolation.py),
[test_ai_strategy_selection.py](../tests/test_ai_strategy_selection.py),
[test_openai_structured_output.py](../tests/test_openai_structured_output.py),
[test_dashboard_paper_activity.py](../tests/test_dashboard_paper_activity.py),
[test_paper_cadence_audit.py](../tests/test_paper_cadence_audit.py).

No service was restarted, no operator migration/database write/control POST was
issued, and no real order, funding change, commit, push or deployment was made.

## 9. Verification follow-up from afc1623 (2026-10-15)

The worktree was clean at `afc1623` before this follow-up. Exact strategy-ID and
version rejection, history-only replay, reason persistence and paper submission
were re-executed, not merely reviewed. Further isolated reproductions confirmed
cancelled-order misclassification, missing pre-signal strategy display and missing
ORB/VWAP configuration binding. Prepared fixes preserve cancellation identity,
render the trace strategy, and reject changed/missing catalog parameters or
post-approval configuration drift with STRATEGY_CONFIGURATION_MISMATCH. No
strategy parameter values or risk thresholds were relaxed.

Read-only preflight again found schema 25, complete migration history 1-25,
intact SQLite/FK/audit checks, persisted US/USD seed 100000 and zero execution
history/exposure. **Entries are currently enabled, not paused.** The API and
dashboard still predate the reviewed commit; migration 26 and all follow-up code
remain unapplied to operator runtime. Separate approval must cover a durable
entry pause before maintenance. No pause, migration, stop/restart or operator
write was performed during verification.

The [migration preflight and acceptance report](PAPER_MIGRATION_ACCEPTANCE.md)
records the exact task-derived database, worker observations, frozen-backup
comparison procedure, supported migration commands, controlled shutdown/restart,
isolated acceptance matrix, supervised paused runtime checklist and data-only
Kite integration plan. It supplements rather than replaces the blockers above.
India still lacks reviewed execution feed/mapping/calendar/cost/funding evidence
and remains BLOCKED; no Kite adapter or broker order integration was added.

Final executed validation (Python 3.11.6, PowerShell, repository root):

```powershell
$env:PYTHONPATH='src;tests'
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest test_protective_exit_isolation test_ai_strategy_selection test_ai_research_schema test_openai_structured_output test_autonomous_paper_trading test_paper_end_to_end test_dashboard_paper_activity test_paper_cadence_audit test_signal_generation test_paper_lifecycle test_paper_order_management test_trade_proposals test_trade_intent test_paper_loop test_india_readiness -q
$env:PYTHONPATH='src'
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest discover -s tests -q
git diff --check
```

- Final focused acceptance: **226 tests passed in 14.081s**.
- Final full discovery: **734 tests ran in 159.153s; OK, five PostgreSQL skips**
  (729 passed, five skipped).
- Supported ops backup/migration/status/audit rehearsal preserved all old table
  records and rebuilt cash on a disposable schema-25 fixture with fills.
- The documented read-only logical comparison was also executed directly before
  and after migration on an isolated fixture: integrity/FKs/data preserved,
  schema 25 then 26.
- Initial reproduction failures were resolved. One initial test selector used
  the wrong dashboard class name; corrected and executed. One rehearsal initially
  failed Windows temporary-file cleanup because sqlite's connection context
  manager does not close the handle; fixed with explicit closing and rerun.
- Expected injected-failure logs and existing AnyIO ResourceWarnings occurred.
  Editor diagnostics for changed source/tests reported no errors; whitespace
  validation passed. No vendor feed, paid model, PostgreSQL, live execution,
  security scan or runtime restart certification is implied.

## 10. b94a8f3 supervised US readiness refresh

Read-only verification from clean `b94a8f3` on October 15 around 06:06 UTC
confirmed the same runtime gaps: schema 25, pre-commit API/dashboard processes,
enabled automatic entries, closed US session and missing execution timestamp.
Integrity/FK/audit checks remain good; persisted US/USD identity, cash 100000
and zero orders/fills/positions remain unchanged. No operator action was taken.

Re-executed 252 focused tests (15.340s) and full discovery of 734 tests (199.814s),
OK with five PostgreSQL skips. Added execution-safety/MVP/portfolio modules to the
focused selection for actual partial fills, cancellation and nonzero fees/P&L.
No production defect was reproduced and no completed fix was repeated.

Section 6 of [PAPER_MIGRATION_ACCEPTANCE.md](PAPER_MIGRATION_ACCEPTANCE.md)
contains the refreshed preflight, approval scope, complete 66-candidate historical
non-order decision matrix and supervised go/no-go checklist. All historical
reasons were recovered without modifying operator rows; missing cycle reasons
were read from linked assessments. US remains NO-GO pending approved maintenance
and session-time feed/protection evidence. The separate next India task is an
offline instrument-master mapping importer/test slice, not India execution.

## 11. Incremental P0 development from assessment d79f358

### Current baseline and verified root causes

October 15 continuation started at exact HEAD
`d79f358086806789f94b81a90f5df3bb7992f740`, with a clean worktree.
That commit contains only the previous readiness report updates; its production
source matches b94a8f3. Existing catalog-ID/version/configuration checks, strict
component-score schemas, serialized model-context guards, preserved rejection
traces and migration 26 were inspected and re-executed, not reimplemented.

Two new producer-level defects were reproduced with deterministic tests:

1. **ORB data eligibility:** `OrbVwapStrategy.evaluate` checked bar-open timestamps
   for freshness, but not bar completion. A breakout four minutes into a five-minute
   bar emitted BUY or SHORT. Opening-range completeness relied on row count: bars
   shifted one minute from the exchange-open grid still emitted BUY.
   The main generated-signal path already filters unfinished bars; the shared
   strategy contract did not. Misaligned ranges could still pass that filter.
2. **Concurrent AI budget admission:** `_reserve_budget` only inspected completed
   usage. Two requests each reserving 264 tokens could enter a provider under a
   264-token budget while the first was outstanding. A coordinated blocking
   provider reproduced the second request succeeding before the first settled.
   Request-envelope/context-size validation was already present; this is a
   cumulative-admission race, not a reason to enlarge the model's context window.

### Completed fixes and scope

- `core/strategies/orb_vwap.py`: returns unpriced HOLD with `INCOMPLETE_BAR`
  before setup evaluation; requires the exact expected opening-range timestamp
  grid, returning `OPENING_RANGE_INCOMPLETE` when it is shifted/incomplete.
  UTC and exchange-local indexes are equivalent. Exact bar-close boundary BUY
  remains valid. Strategy thresholds, risk-price rounding and catalog identity
  are unchanged; invalid bars are not repaired or retimestamped.
- `core/ai/analyst.py`: reserves in-flight request tokens under the existing usage
  lock and settles the reservation atomically with completed usage/error recording.
  Concurrent calls cannot spend the same available allowance. Denied calls return
  `AI_BUDGET_EXHAUSTED` without calling the provider. Terminal provider failures
  release the in-flight reservation, preserving existing failure semantics.
- Regression files: `test_deterministic_strategies.py`, `test_ai_research_schema.py`,
  `test_signal_generation.py`, `test_paper_end_to_end.py`. Tests cover long/short
  unfinished bars, shifted grids, UTC equivalence, the completed boundary,
  persisted NO_SIGNAL/reason/NOT_SUBMITTED, overlapping budget admission and
  failure cleanup.
- The production-composition entry/exit test now follows outcome fill IDs through
  execution signal ID, generated signal, opportunity/assessment and prospective
  decision. It verifies frozen strategy version, decision features, source
  provenance, exit reason and research's NOT_AUTHORIZED_BY_RESEARCH marker.
  Existing learning records were reused, not reconstructed or rewritten.

This is a bounded P0 safety/correctness increment, not a completed multi-market
platform. No migration, runtime task, operator control, provider subscription,
live-order path, funding or strategy-promotion configuration was changed.

### Verified current implementation versus remaining acceptance

| Area | Evidence and boundary |
| --- | --- |
| Research to proposal | Scanner, timestamped snapshots, strict AI assessment, catalog selection, deterministic generation, risk/sizing and persisted proposal paths exist and pass isolated tests. Arbitrary suffixed strategy IDs and unsupported component-score fields remain rejected, not aliased. |
| HOLD/confidence | Operator still has 26 HOLD/0 records: 22 NO_CONFIRMED_BREAKOUT, four PRICE_OFF_TICK. These are abstentions, not zero-confidence orders. Deterministic entry signals also do not supply a calibrated probability; confidence alone is not an authorization gate. Model-reported confidence is advisory; calibrated_probability remains None. No fabricated confidence/price was added. |
| Strategy comparison | ORB/VWAP and SMA trend templates exist; default automatic catalog is ORB only unless explicitly configured. Laboratory/comparison/ranking services support version/config matching, realistic-cost historical evaluation, chronological windows and final-holdout isolation. Paper outcomes and exploratory reports are not independent OOS certification; default runtime strategy eligibility is not an established validated champion/challenger promotion gate. No validated momentum/mean-reversion/news execution family was established. |
| PAPER lifecycle | Production-composition BUY, supported SHORT, cover/stop/target/session exits, idempotency and restart tests pass. Dedicated execution/MVP fixtures cover fees, cash/P&L, partial fills, pending cancellation and independent protection. Full suite also covers partial exit episode accounting. Fill/cost assumptions are simulation models, not certified liquidity/market-impact realism. |
| Decision states | Deterministic NO_TRADE, research rejection, execution blockage, proposal, submission, partial/final fill and cancellation are distinct. A complete market-wide WAITING_FOR_ENTRY/trigger-expiry product contract is not certified by these tests; do not label an unexecuted research row as an order. |
| Learning | Closed episodes persist fills/costs/exit reason and versioned attribution; prospective immutable decision records retain features/evidence and append-only follow-up. known_at/as_of guards, chronological evaluation and reserved holdouts have tests. Calibration, adequate unseen samples, independent review and certified rollback/promotion remain unmet. Legacy unattributed episodes cannot become valid training evidence by guesswork. |
| Markets | Runtime remains single-market US/USD PAPER. India readiness is explicitly BLOCKED; Germany is later. Neither INR funding nor a broker-authorized India feed, reviewed instrument/calendar evidence or simultaneous-worker certification was introduced. |

### Runtime observation, read-only

At 06:41 UTC / 12:11 IST the same API PID 32680 and dashboard PID 14348 were
listening, with October 15 start times 02:32:44 and 02:22:46 IST respectively.
The configured US task still selects `sqlite:///outputs/paper_auto_us.db`,
PAPER/live false, US/USD, one worker and AUTO_MIGRATE=false.
Native SQLite mode=ro/query_only checks found schema 25, integrity ok, no FK
violations, no history-revision components and zero orders/fills/positions/
execution records/outcomes. Runtime GETs report enabled entries, inactive kill
switch, recovery not halted, US CLOSED and missing execution timestamp.
Protection is HEALTHY/recovery-complete at 30 seconds, with zero positions or
pending orders checked: no active-feed capacity certification is implied.

The status inspection initially used a nonexistent candidate `stage` column;
that read-only query failed explicitly and was corrected to the actual `status`
column. Confirmed counts remain 26 NON_ACTIONABLE, 33 RESEARCH_REJECTED, seven
FAILED. This was an inspection-query error, not a schema migration failure.
Neither GETs nor SQLite inspection initialized application services or wrote
operator state. Operator runtime still predates the reviewed/new source.

### Executed tests

Python 3.11.6, declared development requirements inspected. Editor test discovery
returned no tests, so the repository unittest commands were used:

```powershell
$env:PYTHONPATH='src;tests'
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest test_deterministic_strategies test_ai_research_schema -q
# Before fixes: 31 tests, four failed assertions across three tests/subtests.
# Refined race reproduction separately failed because the second request succeeded:
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest test_ai_research_schema.AnalystCostControlTests.test_concurrent_requests_reserve_budget_before_provider_call -q
# After fixes, initial integration selection:
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest test_deterministic_strategies test_ai_research_schema test_signal_generation test_paper_end_to_end test_strategy_pipeline test_trend_strategy test_openai_structured_output -q
# Final focused selection after all test additions:
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest test_deterministic_strategies test_ai_research_schema test_signal_generation test_paper_end_to_end test_strategy_pipeline test_trend_strategy test_openai_structured_output test_ai_strategy_selection test_candidate_research test_protective_exit_isolation test_trade_proposals test_paper_lifecycle test_paper_execution_safety test_paper_mvp_reliability test_outcome_feedback test_prospective_evidence test_strategy_ranking test_strategy_comparison test_strategy_evidence test_strategy_lab test_india_readiness -q
$env:PYTHONPATH='src'
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest discover -s tests -q
```

- Initial post-fix integration: **95 passed, 6.675s**.
- Final focused selection: **266 passed, 94.635s**.
- Full suite: **740 ran, 262.011s; OK with five PostgreSQL skips**
  (735 passed, five skipped).
- Expected injected-provider/recovery/kill-switch errors and preexisting AnyIO
  unclosed-memory-stream ResourceWarnings were emitted. They are not claims of
  operator incidents. No paid-model/feed request or demonstration operator order
  was used.
- Editor diagnostics report no errors in the six changed Python files; Pylance
  verified all four `_record_usage` call sites after its private signature change.
  This is not a repository-wide static/security gate.

### Unmet gates and next development increment

US runtime remains NO-GO until separately approved durable pause, graceful owner
shutdown, WAL-safe verified backup, explicit migration 26, one-worker aligned
restart and paused session-time quote/protection qualification. Follow the
existing [maintenance acceptance procedure](PAPER_MIGRATION_ACCEPTANCE.md);
entry resumption is a separate approval.

Provider credentials/entitlements, remaining quota, measured reconnect/429 paths,
mapping and calendar provenance, operator costs and session-time protection are
still untested operational dependencies. The AI reservation fix is a process-local
admission guard, not a vendor invoice cap: generic providers without output bounds
use estimates, and failures/retries with no reported usage can have unknown billed
tokens. A hard cross-process spending limit requires durable, per-attempt uncertain
usage accounting; no claim of that capability is made here.

The next highest-priority development slice is an **authoritative strategy
eligibility/evidence contract**: bind independently reviewed chronological OOS
reports, instrument/timeframe/market/config/version, costs, sample coverage and
expiry to the selectable catalog; persist precise NO_VALIDATED_STRATEGY reasons
when none qualify, while keeping experimental paper research explicitly separate
from promoted/validated selection. Use the existing lab/ranking/governance
repositories, never automatic promotion or a weaker risk gate. India data-only
mapping certification remains a separate prerequisite, not a runtime shortcut.
$env:PYTHONPATH="src;tests"; python -m pytest -q -p no:cacheprovider tests/test_trade_proposals.py tests/test_autonomous_paper_trading.py tests/test_adaptive_discovery.py
## 12. Automatic-selection policy guard from c3e5e10

Started at clean `c3e5e10e35d6d55e2dedb468fe087671362f900e`.
Inspection confirmed base catalog registration bypassed lab review, and lab
paper activation is not an approved automatic-validation policy: its review
checks use final-holdout metrics and provide no evidence-expiry contract.
Existing advisory ranking and paper samples do not supply that missing approval.

The bounded implementation therefore **blocks**, rather than invents, automatic
qualification. `AutonomousPaperTradingService` requests validated selection from
`AutonomousResearchService`; its request hash persists VALIDATED_AUTOMATIC versus
EXPERIMENTAL_RESEARCH. `CandidateAssessmentService.assess_automatic` preserves
validated snapshot/catalog context and persists NO_VALIDATED_STRATEGY with
AUTOMATIC_VALIDATION_POLICY_UNAPPROVED, NO_TRADE, no recommendation and no AI call.
Cached experimental opportunities cannot be reused during validated resume.
Ordinary research remains explicitly experimental; no automatic fallback exists.
Existing independent execution/risk/readiness/protective gates remain unchanged.
No migration, operator control, funding, restart or live-order capability changed.

Changed production files are `core/ai/candidate_assessment.py`,
`core/autonomous_research.py`, `core/autonomous_paper_trading.py`.
Changed tests are `test_paper_end_to_end.py`, `test_autonomous_paper_trading.py`,
`test_strategy_lifecycle.py`. Existing lifecycle fixtures explicitly select
experimental research in test-only wrappers; the new production-composition
test uses the unwrapped automatic path and verifies persisted rejection,
zero signals/orders/fills/provider calls and restart idempotency. The reviewed
ORB/SMA lab fixture also proves activation does not certify automatic selection.

Executed with Python 3.11.6:

```powershell
$env:PYTHONPATH='src;tests'
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest test_paper_end_to_end test_autonomous_research test_autonomous_paper_trading -q
# 44 passed, 9.011s.
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest test_paper_end_to_end test_autonomous_research test_autonomous_paper_trading test_strategy_lifecycle test_ai_research_schema test_strategy_ranking -q
# First run: 70 tests, two failures from the experimental fixture wrapper.
# After correction: 71 tests, one new fixture unpacking error; both lab-template
# integration subtests passed. Fixed the unpacking error, then ran:
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest test_paper_end_to_end test_autonomous_research test_autonomous_paper_trading test_ai_research_schema test_strategy_ranking -q
# 69 passed, 9.857s.
$env:PYTHONPATH='src'
& 'C:\Users\katka\AppData\Local\Programs\Python\Python311\python.exe' -m unittest discover -s tests -q
# Tool execution interrupted by the subsequent user request; no result captured.
```

The full-suite attempt was not repeated; no full-suite pass or skip count is
claimed for this increment. Expected injected-failure logs and preexisting AnyIO
ResourceWarnings occurred. Changed-file editor diagnostics were clear, and
Pylance found all 13 research.run call sites signature-compatible; these are not
a repository-wide static-analysis gate.

Verdict: **PARTIAL**. Automatic fail-closed enforcement is demonstrated, but no
validated strategy is demonstrated. Valid-evidence acceptance and evidence-specific
expiry/version/market/timeframe/sample tests remain blocked until an authorized
policy defines expiry and compatible independent OOS review without holdout-based
promotion. Existing exploratory version/configuration/data/risk tests are not
misrepresented as that certification. This guard cannot make the old running
worker safe without separately authorized source/schema maintenance.
