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
