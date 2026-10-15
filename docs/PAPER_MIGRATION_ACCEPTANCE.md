# US paper migration preflight and acceptance

Date: 2026-10-15. Reviewed baseline: `afc1623`.
[PAPER_ORDER_AUDIT.md](PAPER_ORDER_AUDIT.md) remains the blocker source of truth.
This follow-up records verification and prepares operator actions; it does not
authorize or perform them. No live broker/order API is used.

## 1. Failed checks and confirmed follow-up defects

Isolated reproductions found three gaps:

1. A cancelled order's candidate trace defaulted to REJECTED, losing cancellation
   identity. It now persists ORDER_CANCELLED / CANCELLED; recorded partial fills
   are not erased. PARTIALLY_FILLED remains explicitly visible as the actual
   order state, with the broader candidate decision SUBMITTED, never FILLED.
2. The candidate table read strategy only from a generated signal, hiding
   strategy identity when evaluation was rejected before signal generation.
   It now uses the persisted trace identity first.
3. Version checks alone did not bind ORB/VWAP configuration. A changed/missing
   frozen catalog hash still generated a signal, and changing parameters after
   proposal approval still submitted a paper order. Both reproductions failed
   before the fix. Dataclass strategy configurations now require the existing
   catalog hash to match before data access, and the current parameters to match
   the persisted deterministic signal before initial submission:
   STRATEGY_CONFIGURATION_MISMATCH. Existing parameter helpers are reused.
   Configuration-less legacy test strategies are not newly reinterpreted as
   versioned configurations.

The exact-ID schema and independent version check from `afc1623` remain intact:
`orb_vwap v1.0.0` is rejected, not stripped, normalized or substituted.
The history-only revision fix passes harmless concurrent-write, genuine mutation,
rollback, missing-trigger, missing-counter/table, missing-page, ordinal-gap and
incomplete-recovery tests. Migration 26 creates one counter and 12 SQLite history
triggers in a transaction; missing components remain fail-closed.

No strategy thresholds, risk limits, quote ages, pause/kill-switch behavior,
holdout rules, account balances or PAPER-only restrictions were relaxed.

## 2. Read-only operator preflight

Observed around 05:33-05:45 UTC (11:03-11:15 IST). API GETs and SQLite reads are
separate snapshots; the existing worker continued running. These are observations,
not a frozen maintenance baseline. Re-run preflight after authorized quiescence.

| Check | Evidence / result |
| --- | --- |
| Configured task | `Start US Paper Auto-Loop API` in [.vscode/tasks.json](../.vscode/tasks.json); `AUTO_MIGRATE=false`, one Uvicorn worker, PAPER, live disabled, US/USD, US_ALL, 10-minute scan, top_n 2. |
| Database selection | Task command explicitly sets `sqlite:///outputs/paper_auto_us.db`. Resolved existing file: `C:\Users\katka\Trading-strategy\outputs\paper_auto_us.db`; not an inferred filename. |
| Running API identity | Port 8000 owner PID 32680, Python 3.11.6 executable, process started October 15 02:32:44 IST. The corresponding US task terminal is present. |
| Dashboard identity | Port 8501 owner PID 14348, same Python executable, started 02:22:46 IST. |
| Source alignment | Both processes predate `afc1623` and these edits. No runtime build hash certifies loaded modules. Do not treat a passing source suite as runtime alignment. |
| Worker/account identity | Persisted PAPER_WORKER_BOUND: market US, base_currency USD, starting_cash 100000. OS lease sidecar exists. Existence is not independent proof of which process holds the lease; no second ownership acquisition was attempted. |
| Schema/history | Versions 1 through 25 present without gaps; maximum 25. Versions 1-16 applied October 12, 17-23 October 13, 24-25 October 14 UTC. Counter/table and all migration-26 triggers absent, as expected before migration. |
| SQLite integrity | Native `mode=ro`, `PRAGMA query_only=ON`; integrity_check = ok; foreign_key_check = no violations. No platform Store/bootstrap opened against operator state. |
| Recovery | GET /recovery: halted false, reasons empty. Loop recovery_failures empty; protective recovery_complete true. No reconcile/resume POST was issued. |
| Entry pause | **NOT PAUSED**: entries_enabled and operator_entries_enabled true. Latest durable control by sequence: PAPER_LOOP_ENTRIES_RESUMED, October 14 15:29:00.600468 UTC. Earlier pause remains in audit history. Approval must include a new durable pause before maintenance. |
| Kill switch | Inactive, no reasons or reported breaching conditions. It was not reset or triggered. |
| Cash/exposure | USD cash and equity 100000; exposure, fees, realized/unrealized P&L zero. No account reset/funding. |
| Execution state | Zero orders, order_events, fills, positions, paper_execution_orders, paper_execution_attempts, execution_records, paper_feedback_fills and execution_reservations. No P&L snapshot exists; cash is also confirmed by API and persisted account seed. |
| Audit integrity | GET /audit: chain_intact true, problems empty; 20 persisted audit entries at inspection. Preserve all entries, including pause, resume and kill-switch history. |
| Current operational gates | US closed; health DEGRADED, no market data received. Protective monitor enabled/HEALTHY, 30s cadence, zero positions/pending orders checked. This does not certify session-time feed/protection behavior. |

The inspected database size was 8,716,288 bytes, but byte size/file hash while
WAL writers run is not an account-preservation proof. Logical snapshot comparison
and SQLite backup are required.

## 3. Operator-only maintenance procedure (NOT EXECUTED)

### Approval and controlled shutdown

1. Obtain explicit approval for the durable entry pause, maintenance window,
   backup, migration 26 and controlled restart. Approval to inspect is not approval
   to perform any of these actions. Preserve any active kill-switch reason.
2. Re-identify port owners and task terminals; PIDs above are historical, not
   hard-coded stop targets. Review orders/positions. If positions or pending
   orders now exist, stop here and agree a protection plan; never kill protection
   simply because the window has started.
3. Through the existing authenticated dashboard control or
   `POST /paper/loop/control`, record:
   `{"entries_enabled":false,"operator":"<reviewed operator>","reason":"Approved migration-26 maintenance"}`.
   Verify GET /paper/loop and latest audit action PAPER_LOOP_ENTRIES_PAUSED.
   This pauses automatic entries, not protective exits. Do not reset the kill
   switch or resume failed recovery.
4. Stop the dashboard via Ctrl+C in its owning Streamlit console. For the API,
   send **one Ctrl+C in the `Start US Paper Auto-Loop API` task terminal**.
   Uvicorn must run normal ASGI shutdown: paper loop/protection stops and joins,
   kill monitor stops, providers close, then worker lease releases. Wait for
   "Application shutdown complete" and process exit; verify ports and exact PIDs
   are gone. Do not use Task Terminate/Stop-Process as a claimed graceful shutdown.
   If shutdown fails/stalls, retain ownership, do not launch a replacement, and
   investigate. Forced termination needs separate approval/recovery review.
5. Do not delete the ownership sidecar or WAL/SHM files. Do not create a second
   worker to probe ownership. Once all writers have exited, re-run database
   integrity/audit/account checks and capture a frozen logical manifest.

### Backup and supported migration commands

These are review-only commands, run from the repository root after approval and
successful quiescence. Set `$python` to the reviewed VS Code interpreter executable
(the executed tests used Python 3.11.6), not an unverified PATH interpreter.
Never paste credentials into commands or change currency/funding.

```powershell
$python = '<reviewed Python executable>'
$db = (Resolve-Path -LiteralPath 'outputs\paper_auto_us.db' -ErrorAction Stop).Path
$env:PYTHONPATH = 'src'
$env:DATABASE_URL = 'sqlite:///' + $db
& $python -m stockmarket.ops status
# Expected pre-migration: applied 1..25, pending [26], exit 1 for pending.

$backupDir = Join-Path 'backups' ('us-migration26-' + (Get-Date -Format 'yyyyMMddTHHmmssfff'))
if (Test-Path -LiteralPath $backupDir) { throw 'Backup directory already exists; stop' }
& $python -m stockmarket.ops backup --dir $backupDir --keep 1
if ($LASTEXITCODE -ne 0) { throw 'Backup failed; do not migrate' }
$backupFile = (Get-ChildItem -LiteralPath $backupDir -Filter 'trading-*.db').FullName
# Run the read-only comparison below before migrating.
& $python -m stockmarket.ops verify-audit
if ($LASTEXITCODE -ne 0) { throw 'Audit verification failed; stop' }
# Only after backup/account comparison and explicit migration approval:
& $python -m stockmarket.ops migrate
if ($LASTEXITCODE -ne 0) { throw 'Migration failed; do not start services' }
& $python -m stockmarket.ops status
if ($LASTEXITCODE -ne 0) { throw 'Unexpected schema state; stop' }
& $python -m stockmarket.ops verify-audit
if ($LASTEXITCODE -ne 0) { throw 'Audit verification failed; stop' }
```

`stockmarket.ops` opens writable connections even for status/verify-audit. These
commands belong to authorized offline maintenance, not this read-only preflight.
Its backup uses SQLite's backup API (WAL-safe), not a raw copy of the database.
It also prunes backups matching its pattern: a fresh unique directory with keep=1
avoids pruning preexisting operator backups. Do not reuse a populated directory.

Run this read-only logical comparison both **before** and **after** migration,
using the same reviewed backup. It verifies all old table data, including
audit/identity/controls/history; only new migration-26 objects/row may differ.
It must report schema 25 before, schema 26 after, no FK errors and intact data.

```powershell
@'
import hashlib, json, sqlite3, sys
from contextlib import closing
from pathlib import Path

def inspect(path):
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as c:
        c.execute("PRAGMA query_only=ON")
        c.execute("BEGIN")
        assert c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        versions = [r[0] for r in c.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
        assert versions in (list(range(1, 26)), list(range(1, 27)))
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            if r[0] != "execution_history_revision"]
        manifest = {}
        for table in tables:
            name = table.replace('"', '""')
            rows = c.execute('SELECT * FROM "' + name + '"').fetchall()
            if table == "schema_migrations":
                rows = [row for row in rows if row[0] <= 25]
            encoded = sorted(json.dumps(row, default=lambda x: x.hex()) for row in rows)
            manifest[table] = (len(rows), hashlib.sha256(
                json.dumps(encoded).encode()).hexdigest())
        if versions[-1] == 26:
            assert c.execute("SELECT singleton,revision FROM execution_history_revision").fetchall() == [(1, 0)]
            expected = {f"execution_history_{t}_{e}" for t in
                        ("orders","order_events","fills","paper_feedback_fills")
                        for e in ("insert","update","delete")}
            installed = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
            assert expected <= installed
        return versions[-1], manifest

source_version, source = inspect(sys.argv[1])
backup_version, backup = inspect(sys.argv[2])
assert backup_version == 25
assert source == backup, "Preexisting table data changed; do not restart"
print("Integrity/FKs/account/history preserved; schema", source_version)
'@ | & $python - $db $backupFile
if ($LASTEXITCODE -ne 0) { throw 'Snapshot verification failed; stop' }
Get-FileHash -LiteralPath $backupFile -Algorithm SHA256
```

Revision zero is expected immediately after offline migration, before workers
resume. Do not reset a nonzero counter to pass this check. Keep the backup and
manifest outside Git under restricted local access. If verification fails, leave
services stopped; restoration needs separate approval and must not overwrite or
discard newer state implicitly. There is no supported down-migration command.

### Restart and supervised runtime acceptance

1. Review the intended source commit plus these uncommitted fixes. Retain the
   exact US task, database, US/USD identity, instrument master and one worker.
   Use `Start US Paper Auto-Loop API` with AUTO_MIGRATE=false. Never use the India
   personal-research task as a substitute, enable reload/multiple workers, or
   change seed/market/credentials silently.
2. Confirm source path/interpreter from the startup console, required schema 26,
   persisted US/USD identity, PAPER execution and clean startup recovery. Absence
   of a runtime build hash remains a verification limitation; preserve startup
   evidence. Check no duplicate process/worker exists.
3. Confirm **entries remain paused** from the durable control and independent
   protection is enabled/healthy. If either is false, do not authorize entries.
4. Start `Start platform PAPER dashboard` using the same interpreter, API URL and
   token file. Confirm PAPER banner, compatible API contract and read-only traces.
5. Compare account cash/positions/orders/fills to the frozen baseline; startup
   audit/control additions must be explained, never deleted to force equality.
6. During an official regular US session, separately authorize bounded data-only
   quote observations while entries stay paused. Check canonical mapping, source
   timestamps/ages, session/calendar, entitlements, 429/timeout behavior and
   actual quota/protective headroom. Status GETs do not fetch/refresh quotes.
7. Only after source/recovery/feed/protection evidence passes, separately approve
   resuming a small supervised PAPER trial. An eligible deterministic signal,
   risk approval, quote and all remaining gates are still required. No forced
   demo order is permitted. Observe order events, fills, audit and reconciliation.
8. On any readiness failure, keep/pause entries while preserving safe protective
   monitoring; never reset recovery/kill-switch gates as a workaround.

## 4. Repeatable isolated acceptance matrix

All cases use deterministic mocks/in-memory or temporary fixture databases.
No test selects the operator database, vendor credentials or live order API.

| Case | Executed evidence / expected outcome |
| --- | --- |
| HOLD/NO_TRADE | `test_paper_end_to_end`, `test_trade_intent`, `test_dashboard_paper_activity`: no order/fill; explicit abstention rather than actionable signal. |
| Eligible BUY | End-to-end production composition submits/fills PAPER, links persistence/audit, restores after restart and deduplicates retry. |
| Eligible/ineligible SHORT | End-to-end short/cover requires allow_short; `test_trade_intent.test_unshortable_short_sell_is_rejected` enforces instrument shortability; risk/quantity/cash remain required. No blanket permission for shorts. |
| ID/version/configuration | Exact schema enum and unknown-ID rejection; signal version mismatch before data access; changed/missing parameter hash before data access; changed post-approval parameters before submission. |
| Quotes/session | Missing/stale quote traces retain exact codes; closed/weekend/calendar cases do not scan/submit; protective stale quotes fail. |
| Cash/duplicates | `test_trade_proposals` minimum-lot cash rejection; order/intent tests verify idempotency and conflicting keys; end-to-end repeated cycles cannot add fills. |
| Kill switch/pause/recovery | End-to-end kill rejection; loop durable pause preserves exits; interrupted replay cannot resume; concurrent mutation/missing revision/page blocks entries. |
| Order states | Persisted candidate and rendered table check SUBMITTED, PARTIALLY_FILLED, FILLED, REJECTED and CANCELLED separately, retaining actual quantities/states. |
| Protective independence | Threaded entry-research failure still permits valid stop exit; missing/expired quote and changed portfolio invalidate prepared exits. Existing deterministic final risk and session checks remain. |
| Migration rehearsal | Supported `ops.backup`, `ops migrate/status/verify-audit` on a disposable schema-25 fixture with orders/fills/account audit identity; old tables unchanged, integrity good, cash rebuilt identically, revision zero. |
| India fail-closed | `test_india_readiness`: unreviewed/missing calendars, missing mapping/feed evidence, zero/default/USD funding, mismatched identity and incomplete replay cannot authorize entries. |

Final test results and commands are recorded in the follow-up section of
[PAPER_ORDER_AUDIT.md](PAPER_ORDER_AUDIT.md). PostgreSQL skips are not passes.

## 5. Data-only Kite Connect integration plan (NOT IMPLEMENTED)

Existing boundaries reviewed:
[MarketDataProvider](../src/stockmarket/core/data/provider.py),
[factory](../src/stockmarket/core/data/factory.py),
[capabilities](../src/stockmarket/core/data/capabilities.py),
[quality](../src/stockmarket/core/data/quality.py),
[resilience](../src/stockmarket/core/data/resilient.py),
[worker lease](../src/stockmarket/core/paper_worker.py) and existing India readiness
tests. No Kite adapter is registered. Do not add a broker execution adapter.

1. **Credentials/entitlements:** use the existing secret-file/environment boundary
   for API key, API secret and short-lived access token, with restricted local
   access, no DB/Git/browser exposure. Review interactive broker login/token
   exchange separately; do not automate passwords/OTP. Confirm actual NSE/BSE
   cash-equity streaming/historical entitlements and allowed use in writing.
   Public documentation or possession of a key is not entitlement evidence.
   Redact authorization headers and WebSocket query tokens from logs/errors.
2. **Interface:** a future data-only adapter implements get_instrument, get_quote,
   get_ohlcv and calendar-derived get_market_status. Return canonical Quote and
   sorted UTC-indexed OHLCV; translate only supported intervals (e.g. 5m to
   5minute), reject unsupported/depth requests before network. Verify actual
   historical coverage, bounded chunks, gaps, bar completion and adjustment
   semantics; never pad missing candles with invented values.
3. **Streaming:** keep a bounded per-instrument cache of full-mode packets with
   source/exchange timestamp and separate receive time. Heartbeats establish
   connection liveness, not quote freshness. LTP/quote packets without usable
   source time cannot authorize entries. On disconnect, invalidate entry
   readiness, reconnect with bounded backoff and replay reviewed subscriptions.
   Require fresh identity-checked packets after reconnect; duplicates,
   out-of-order/future/naive times and stale prices fail closed. Do not replace
   source timestamps with now. Ignore broker order-update messages as execution
   evidence; paper fills come only from the platform paper ledger.
4. **Failures/quotas:** map expired/invalid token (403/TokenException) to
   ProviderAuthenticationFailed and require reauthentication, not infinite retry.
   Map missing entitlement, missing quote keys, unsupported symbol, timeout and
   429 to existing explicit provider errors. Review current endpoint/account
   quotas and instrument/connection limits; measure actual calls/retries and
   reserve capacity for the independently scheduled protective lane. Local
   thread separation is not vendor-credit reservation. HTTP/stream fallback is
   allowed only if independently validated; research-only fallback never
   authorizes an entry.
5. **Instrument master:** review each NSE EQ -> XNSE and BSE EQ -> XBOM mapping;
   bind exchange + tradingsymbol, instrument_token, exchange_token, segment,
   master snapshot/version, tick, lot, INR and Asia/Kolkata. Canonical IDs remain
   MIC:SYMBOL. Tokens are transport identities, not permanent canonical keys.
   Instrument-dump last_price is not a live quote. Detect token reassignment,
   symbol changes, suspension/delisting, splits/mergers and corporate-action
   adjustment changes; block until reviewed and reconcile existing positions.
   Do not derive shortability from EQ or NSE/BSE membership.
6. **Calendar:** obtain official cash-equity exchange circulars/holiday schedules
   separately for XNSE/XBOM; archive checksum/source/date coverage and independent
   review, including announced special sessions and revisions. Use the existing
   per-venue reviewed-calendar contract; no invented holiday/Muhurat dates.
7. **Account/risk:** separately approved new INR paper database and explicit
   positive INR seed, one IN execution worker and persisted identity. Never
   repurpose the US account, fund by implicit USD conversion or claim simultaneous
   US/India worker certification. Review cash-only buying power, tick/lot rounding,
   brokerage, STT, exchange/SEBI charges, GST, stamp duty, applicable delivery
   charges, spread/slippage and fee rounding with authoritative current evidence.
   Review instrument/product-specific intraday short eligibility and session
   flattening; no unrestricted overnight shorts or presumed broker leverage.
8. **Acceptance before integration promotion:** deterministic HTTP/stream fixtures
   for auth/entitlement/429/disconnect, heartbeat-only cache, reconnect without
   fresh packet, missing mapping, token reuse, timezones/bar gaps/corporate actions,
   tick/lot failures, invalid calendars, insufficient/mismatched INR funding,
   short restrictions, fees, recovery and worker conflicts. Prove zero orders and
   exact rejection codes for every unverified case. Capture separately reviewed
   data-only session evidence; do not flip provider_validated/mapping flags merely
   because an adapter exists. India execution stays BLOCKED until all evidence
   and acceptance gates are complete.

Official API references reviewed (not proof of this operator's subscription):
[quotes/instruments](https://kite.trade/docs/connect/v3/market-quotes/),
[historical candles](https://kite.trade/docs/connect/v3/historical/),
[streaming](https://kite.trade/docs/connect/v3/websocket/),
[authentication](https://kite.trade/docs/connect/v3/user/),
[exceptions](https://kite.trade/docs/connect/v3/exceptions/).
Only market-data endpoints are planned; **no broker live order API calls**.
