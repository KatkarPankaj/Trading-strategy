# Master Architecture Compliance Audit

## Executive verdict

**Verdict: NOT READY for autonomous trading. Suitable only for constrained, manually supervised PAPER research and order workflows through the platform API. Not suitable for live trading.**

The repository has meaningful foundations: provider-independent domain contracts, timezone-aware markets and sessions, fail-closed data checks, a deterministic proposal pipeline, a separate risk-gated order service, paper execution, persistence/recovery primitives, and explicit live-readiness gates. These are components and a working paper service—not a fully integrated, independently operated trading platform.

The most important architectural gap is inconsistent enforcement across entry points. The platform API routes orders through `TradingService` and `RiskEngine`, but the root legacy dashboard still has a separate paper-account mutation path in `_execute_paper_order`. The proposal workflow is manual and process-local; it is neither a durable autonomous decision loop nor a restart-safe proposal queue. The default bootstrap composes a paper broker; no concrete live broker adapter is present. Calendar coverage, execution-grade data, FX controls, operational integration, and validated deployment are also incomplete.

**Mode decision**

| Mode | Verdict | Basis |
|---|---|---|
| `PAPER_MANUAL` | **Conditionally usable for supervised local evaluation** | The platform API supports authenticated manual paper orders, explicit proposal acceptance and RiskEngine evaluation. Keep the legacy dashboards out of any claim of centralized risk enforcement; their state and order path are separate. |
| `PAPER_AUTONOMOUS` | **BLOCKED** | There is no autonomous scheduler/runner with durable proposal state, portfolio-wide serialized decision cycle, restart-safe deduplication, measured unattended paper evidence, and verified monitoring/kill-switch wiring. |
| `LIVE_AUTONOMOUS` | **BLOCKED; no live deployment authorized or demonstrated** | No concrete live adapter exists; bootstrap constructs paper services; live readiness is a checklist foundation and cannot pass without real, verified components. Data, audit, operations, and autonomous-paper prerequisites are also unmet. |

**Overall score: 50.2 / 100** (108 / 215 points; 43 equally weighted areas, each scored 0–5). This measures integrated capability against the requested target architecture, not code volume, test count, or individual component quality. A prototype can contain strong foundations and still be unready for unattended operation.

## Scoring and rating method

- Scores: **0** missing; **1** rudimentary/isolated; **2** partial or materially incomplete; **3** useful foundation/integration with significant limitations; **4** substantially integrated with bounded gaps; **5** complete, operationally integrated, and evidenced against the target.
- Compliance labels describe the requirement, not the score: `COMPLETE`, `MOSTLY COMPLETE`, `PARTIAL`, `FOUNDATION ONLY`, `MISSING`, `ARCHITECTURALLY WRONG`, or `LEGACY ONLY`.
- An abstraction, checklist, test, or optional branch is not scored as a production capability unless the active runtime uses it and the end-to-end behavior is demonstrated.
- Evidence below is based on source and documentation inspection. This audit did not run tests, connect providers, exercise deployment, or verify any external account. Previously reported local tests (251 passing) are historical context only, not fresh audit evidence.
- The repository’s earlier architecture and phase documents are evidence of current implementation claims, not authority to override the greenfield target or the required fail-closed, paper-first boundaries.

## 43-area assessment

| # | Architecture area | Rating | Score / 5 | Evidence and gap |
|---:|---|---|---:|---|
| 1 | Separation of domain, application and infrastructure concerns | PARTIAL | 2 | `core` contains useful service/domain boundaries, but root dashboards retain trading and persistence logic; platform and legacy paths are not consolidated. |
| 2 | Instrument identity and trading constraints | MOSTLY COMPLETE | 4 | `Instrument` models market, MIC/exchange, currency, timezone, tick/lot/minimum quantity, shortability and status. Coverage of asset-specific contract multipliers and full broker constraints is not demonstrated. |
| 3 | International market definitions | PARTIAL | 3 | Registry supports US, India and Germany/Xetra with explicit local currencies, timezones and MICs; only a small static market set and equity-focused assumptions are present. |
| 4 | Exchange calendars and coverage | PARTIAL | 2 | Calendar abstraction handles holidays, early closes and pauses, and fails closed for uncovered years. Only US and DE 2026 files exist; India and subsequent years are uncovered. |
| 5 | Timezone and point-in-time discipline | MOSTLY COMPLETE | 3 | Core research/proposal contracts validate aware timestamps and constrain evidence to `as_of`; backtest execution uses later-bar latency. Historical provider revision/correction controls and universal end-to-end timestamp lineage remain incomplete. |
| 6 | Provider-independent market-data contract | PARTIAL | 3 | Abstract quote/OHLCV/status contract and Yahoo/mock implementations exist. Current sources are research-only; there is no demonstrated execution-grade multi-provider feed. |
| 7 | Market-data quality validation | MOSTLY COMPLETE | 4 | Quote and bar checks include identity, freshness, timezone, OHLC consistency, finite values, duplicate/order/gap checks, frozen prices and tick-grid checks in the resilient wrapper. Vendor-specific correctness and corporate-action provenance remain external. |
| 8 | Market-data resilience and failure behavior | PARTIAL | 3 | Retries, timeout wrapper, rate-limit handling, circuit breaker and fail-closed quality errors exist. Provider-specific policy behavior, bounded resource lifecycle under load, and operationally verified failover are not established. |
| 9 | Data licensing, execution suitability and provenance | FOUNDATION ONLY | 1 | Yahoo/mock are marked research-only and provider fields are recorded, but execution-grade feeds, licensing review, immutable raw-data snapshots and durable dataset lineage are absent or unverified. |
| 10 | News event normalization and freshness | PARTIAL | 3 | News events are typed; Finnhub evidence is bounded, instrument-matched and timestamp-checked. Coverage, source quality, deduplication across vendors and event-correction/version history are limited. |
| 11 | Regime analysis | PARTIAL | 3 | Deterministic regime labels/scores use quality-checked bars with an explicit observation window; model calibration, cross-market validation and durable regime history are not shown. |
| 12 | AI advisory boundary and output validation | MOSTLY COMPLETE | 3 | AI is explicitly research-only, prompt input is bounded/sanitized, schemas forbid extra fields, and strategy candidates are constrained to a known list. Provider reliability, prompt/model version governance, independent evaluation and calibration are incomplete. |
| 13 | Deterministic strategy contracts and selection | PARTIAL | 3 | Strategy protocol returns signals, ORB/VWAP is deterministic, and AI ranks only registered strategies. Registry governance is not integrated into a full paper-autonomous promotion loop. |
| 14 | Signal aggregation and directional authority | PARTIAL | 3 | Aggregator is deterministic and the documented pipeline requires a matching technical signal; signal/freshness and identity are checked. Legacy callers and all historical/live-like evaluation paths are not uniformly migrated. |
| 15 | Explainable `TradeProposal` contract | PARTIAL | 3 | Proposal records include rank, timestamps, signal, regime, research evidence and explanation; risk is explicitly `NOT_EVALUATED`. Proposals are not durable and explanations are not independently assessed for completeness or usefulness. |
| 16 | Cross-instrument opportunity ranking | FOUNDATION ONLY | 2 | Ranking is deterministic and tie-broken, but only up to 10 candidates are accepted and each is processed sequentially through a complete pipeline. Portfolio-level optimization, sector/correlation constraints across candidates and latency budgets are absent. |
| 17 | Manual paper proposal acceptance | PARTIAL | 3 | Authenticated API acceptance supplies explicit quantity and routes stored signal context through `TradingService`; RiskEngine remains final gate. Context is an in-memory bounded cache, quantity is operator-supplied, and declared actor is audit metadata rather than identity/RBAC. |
| 18 | Paper-autonomous operation | MISSING | 0 | No scheduled autonomous proposal-to-order loop, durable queue, supervised rollout, automatic bounded sizing workflow, or completed unattended-paper acceptance evidence was found. |
| 19 | Position sizing and order constraints | PARTIAL | 2 | `size_position()` implements stop-distance risk sizing and cash/notional/liquidity/broker caps. Proposal submission currently accepts caller-supplied quantity; sizing is not the integrated pre-risk policy for every paper entry. |
| 20 | Deterministic per-order risk gate | MOSTLY COMPLETE | 3 | `TradingService.submit()` evaluates before `OrderManager`; missing limits/data and invalid orders reject; paper signal submission is mode-restricted. This guarantee applies to the platform service, not every legacy entry point. |
| 21 | Portfolio-wide risk controls | PARTIAL | 3 | Limits cover daily loss, drawdown, risk/trade, notional, sector/correlation exposure, leverage, rate and liquidity. Risk state is caller-supplied and depends on accurate service wiring; the legacy paper ledger does not use this centralized engine. |
| 22 | Portfolio accounting and valuation | PARTIAL | 2 | Platform portfolio tracks local/base currency cash, positions, exposure, PnL and drawdown. FX rates are explicitly configured floats without demonstrated freshness, bid/ask spread, source provenance or revaluation controls. |
| 23 | Broker-independent order lifecycle | PARTIAL | 3 | Order manager has status transitions, client-id idempotency, risk approval requirement, event tracking and uncertain-submission safeguards. Multi-process concurrency/locking and production broker semantics are unverified. |
| 24 | Paper broker/execution realism | PARTIAL | 3 | Paper broker simulates orders/fills and the backtest execution model includes latency, spread, slippage, fees, participation and corporate actions. PaperBroker is in-memory; default market-open callback is permissive unless wired; no realistic queue/market impact model is established. |
| 25 | Concrete live broker integration | MISSING | 0 | `BrokerAdapter` and live-client protocols exist, but no concrete live broker implementation was found. Bootstrap explicitly builds `PaperBroker`. |
| 26 | Trading-mode separation and live fail-closed gates | MOSTLY COMPLETE | 3 | PAPER is default; live requires explicit mode, enablement, phrase, readiness checks and halt callback; Compose hard-pins PAPER. The live path is not operationally proven and there is no adapter to exercise it. |
| 27 | Restart recovery and broker reconciliation | PARTIAL | 3 | Persisted orders/events restore; startup reconciliation halts on discrepancies; position replay and explicit acknowledged resume exist. Recovery was not run against an external broker; portfolio snapshot/account state, crash injection and repeated restart scenarios are not demonstrated. |
| 28 | Transactional persistence and schema evolution | PARTIAL | 2 | SQLite/PostgreSQL abstractions, transactions, versioned migrations and repositories exist. Order transition persistence and fill/account state are distributed across callbacks; atomicity across risk, order, fill, portfolio and audit writes is not established as one durable transaction. |
| 29 | Auditability and decision reconstruction | PARTIAL | 3 | Hash-chained audit and order audit can reconstruct market data, signals, sizing, risk, broker response and fills. Completeness is optional in paper, AI/proposal provenance has gaps, and audit integrity/restore procedures have not been operationally verified. |
| 30 | Strategy/configuration versioning and approvals | FOUNDATION ONLY | 2 | Parameter hashes, candidate/approval/active states, human separation and live checks exist. Training evidence and registry interfaces are not a completed deployment/promotion process; no continuous drift/rollback workflow is established. |
| 31 | Backtest causality and execution model | PARTIAL | 3 | Event-driven bars, no same-bar entry fills, next-bar latency, sizing/portfolio accounting and cost/corporate-action models exist. Inputs include arbitrary signal callbacks and caller FX/actions; point-in-time data, delisted-universe and calibrated market impact are not demonstrated. |
| 32 | Walk-forward and held-out evaluation | FOUNDATION ONLY | 2 | Train/validate/test windows, embargo and separate test confirmation functions exist. They are callable utilities rather than mandatory gates in strategy approval or production configuration. |
| 33 | Overfit, multiple-testing and robustness controls | FOUNDATION ONLY | 2 | Candidate scoring includes OOS metrics, stability, sensitivity, warning/disqualification signals and testing-count warnings. Statistical controls are heuristic; no empirical strategy certification or enforced promotion threshold integration is demonstrated. |
| 34 | Learning and parameter-change governance | FOUNDATION ONLY | 2 | Evidence assessment and human approval/version states exist; automated review explicitly does not alter parameters. Production-grade evidence dataset lineage, independent approvals and rollback/drift controls are incomplete. |
| 35 | API security and operator identity | PARTIAL | 2 | Bearer token uses constant-time comparison, protected endpoints fail unavailable without configured token, and Compose binds loopback. Authorization is a shared token, not user identity/RBAC; rate limiting, remote TLS termination and security operations are external/unverified. |
| 36 | Dashboard and operator workflow | PARTIAL | 2 | API-backed dashboard offers advisory research, ranked opportunities, explicit paper acceptance, order/risk review and mode banner. Legacy dashboards remain active independent implementations and need separation/retirement or migration governance. |
| 37 | Health, metrics, structured logs and alerts | PARTIAL | 2 | Health checks, Prometheus-style metrics, structured/redacted logs, alert hooks and error counts exist. Most state is process-local; delivery channels, alert-to-kill-switch wiring and monitoring/alert SLO operation are not demonstrated. |
| 38 | Kill switch and automatic safety triggers | FOUNDATION ONLY | 2 | Gate, manual/automatic kill switch, trigger policies, optional order cancellation and persisted events exist. Automatic monitoring requires explicit startup/wiring; process-local gate, scheduler supervision and tested fail-safe behavior across restart are incomplete. |
| 39 | Secret/configuration and input security | MOSTLY COMPLETE | 3 | Secret wrapper/file support, config secret detection, redaction, HTTPS checks, bounded inputs and parameterized SQL are present. Threat modeling, identity/access policy, external penetration validation and operational secret rotation are not evidenced. |
| 40 | CI, dependency/security scanning and test automation | PARTIAL | 3 | CI workflow runs unit tests, Bandit and pip-audit with pinned Python. Audit did not run CI; no evidence here of successful current GitHub run, integration environment, deployment gate, or reproducible broker/provider validation. |
| 41 | Deployment, backup and recovery operations | PARTIAL | 3 | Docker/Compose uses non-root app, read-only filesystem, PostgreSQL, explicit migration, loopback binding and optional database backup. No deployed environment, off-host backup proof, restore drill, TLS proxy, high availability or rollback exercise was verified. |
| 42 | Concurrency, throughput and horizontal scaling | FOUNDATION ONLY | 1 | Single-worker deployment is documented because state is process-local; opportunity limit is 10 and per-instrument processing is sequential. No durable distributed queue, shared gate/state coordination, load results or capacity limits were demonstrated. |
| 43 | End-to-end operational readiness and evidence | FOUNDATION ONLY | 2 | A coherent local PAPER API stack and extensive unit coverage have been built, but no verified current CI/deployment, external-provider credentials, end-to-end recovery drill, autonomous-paper run, or live broker proof supports production readiness. |

### Score roll-up

| Dimension | Result |
|---|---:|
| Architecture areas scored | 43 |
| Points earned | 108 |
| Maximum points | 215 |
| Normalized score | **50.2 / 100** |
| Areas rated `MISSING` | 2 |
| Areas rated `FOUNDATION ONLY` | 8 |
| Areas rated `ARCHITECTURALLY WRONG` | 0 |
| Areas rated `LEGACY ONLY` | 0 |

Some risks are captured as `PARTIAL` rather than `ARCHITECTURALLY WRONG` because a correct platform boundary exists, but it is not universally enforced while the legacy dashboard remains an independent paper workflow.

## Mode blockers

### Before any `PAPER_AUTONOMOUS` pilot

1. **One order boundary:** disable or explicitly isolate legacy order-producing dashboards, then prove every supported paper entry routes through `TradingService` and `RiskEngine`; reject entries when service, risk limits, quote, calendar, or gate state is unavailable.
2. **Durable autonomous decision state:** persist proposal and decision inputs/results, deduplication keys, run identity and timestamps. On restart, do not recreate or resubmit an ambiguous action; require recovery/reconciliation before resuming.
3. **Explicit, deterministic sizing:** integrate sizing constraints from validated account/portfolio/market data before the final RiskEngine evaluation. Size must be explainable, bounded, instrument-aware and reject unknown liquidity/FX/constraints.
4. **Serialized portfolio-wide decisions:** make position and exposure checks atomic with submission, including concurrent candidates and duplicate proposals. A per-request 10-candidate loop does not supply portfolio-level ranking or reservation.
5. **Supervised operation:** wire the kill switch and stale-data/broker/database/limit triggers into the running process; demonstrate trigger, cancellation policy, restart persistence, explicit reset and exit allowance under fault injection.
6. **Evidence and acceptance gate:** define and meet minimum unattended PAPER duration/trade volume, operational error and reconciliation criteria, plus a human review/rollback process before expanding scope.
7. **Calendar and data coverage:** supply verified calendars for each enabled market/year; use a suitable timestamped feed, document licensing/use, validate data outages/corrections and ensure all research/order timestamps remain causal.
8. **Test the actual runtime:** CI success, application/API smoke test, persistence/restart recovery, order idempotency, failure injection and backup restore must be observed in the target deployment environment. Existing unit test count alone is not acceptance.

### Before any `LIVE_AUTONOMOUS` consideration

All PAPER_AUTONOMOUS blockers are prerequisites. In addition:

1. Implement and independently validate a concrete broker adapter with account/market/instrument verification, client-order idempotency, uncertain-submit recovery, order modification/cancel, fill semantics, supported-market declarations and sandbox testing.
2. Acquire execution-grade, licensed market data with freshness, sequence/correction handling, source failover and broker/data-price reconciliation. Research-only Yahoo/mock providers must remain rejected for live readiness.
3. Establish production identity and least-privilege authorization (not a shared bearer token), network/TLS policy, credential rotation, audit retention and incident response.
4. Run live-readiness checks against actual connected broker/account, fully migrated database, synchronized clock, current market data/calendar, reconciled positions/orders, approved/version-verified strategies, risk limits, kill switch and completed paper criteria. A protocol or manually constructed boolean checklist is not proof.
5. Demonstrate end-to-end disaster recovery, restore, reconciliation, kill-switch and controlled rollout/rollback in a production-like environment. Define human authorization and explicit separation between operator and approver.
6. Establish monitored operational ownership: on-call/escalation, alert delivery, limits, logs/metrics retention, service capacity, backups, change management and incident procedures. A live endpoint or environment flag is not a live system.

## Target architecture gaps

- **Single authoritative execution path:** root legacy dashboard order logic mutates its own Streamlit paper state and does not invoke the platform `RiskEngine`; the newer API dashboard is not a migration of those behaviors.
- **Proposal durability:** the proposal submission context is an in-process `OrderedDict`, capped at 1,000 and lost on restart. Research results are not a durable, queryable decision queue.
- **Portfolio coordination:** the API deployment is deliberately one worker, and candidate analysis is sequential. There is no shared reservation/locking layer that atomically combines simultaneous risk checks and orders across workers or processes.
- **Sizing integration:** an independent sizing helper exists, while manual proposal acceptance requires a caller quantity. The final RiskEngine can reject excess risk, but no standard sizing policy automatically determines a safe order size in the proposal workflow.
- **FX and market completeness:** FX uses configured rates without observed quote time/source/spread; the India calendar is uncovered, which correctly blocks the dates but prevents usable India-market automation.
- **Data and AI governance:** news/AI scoring and strategy selection remain advisory, with no calibration/evaluation history, prompt/model release approval, vendor data agreement record or continuous monitoring for performance/drift.
- **Production runtime:** Compose is a hardened local/single-process starting point, not demonstrated deployment. Optional Redis is not used; no horizontal coordination or externally verified alert route is shown.
- **Backtest-to-production consistency:** backtest engine and current strategy contract are not proven to be the same strategy implementation in an enforced research/approval pipeline; test-period results are not wired into config activation.

## Technically ordered implementation sequence

1. **Close execution-boundary inconsistency.** Inventory every executable entry point; isolate legacy dashboards from order placement or route them through the platform service. Add architecture tests asserting that all supported new-entry paths require RiskEngine approval and PAPER mode.
2. **Complete domain coverage and point-in-time provenance.** Obtain/version verified exchange calendars per enabled market; formalize FX quotes with timestamp, source and spread; retain data snapshots/references and policy/version identifiers needed to reproduce a decision.
3. **Unify sizing and risk inputs.** Integrate the existing sizing calculator into a single paper proposal-acceptance service using trusted account state, validated quote/stop, costs, instrument constraints and portfolio exposure. Keep RiskEngine as final independent gate; atomically reserve/check portfolio limits with order creation.
4. **Persist orchestration and idempotency.** Add durable proposal/run records, stable source and model identifiers, acceptance state transitions and idempotency. Design crash-safe write boundaries and do not silently regenerate submission state after uncertain completion.
5. **Make paper automation explicit but disabled by default.** Add a supervised scheduler/worker with a strict PAPER-only configuration, per-cycle candidate budget, action limits, manual rollout switch, no-order dry run and hard dependency on fresh data, current calendar, healthy persistence and open safety gate.
6. **Prove safety and recovery.** Test concurrency, duplicate/replay behavior, stale/future data, risk rejection, process crash around submission, broker disconnect, database failure, kill switch, restart/reconciliation, backup restore and risk-reducing exits. Observe successful CI and a prolonged unattended paper pilot before widening deployment.
7. **Operationalize paper service.** Deploy to a controlled paper environment with real alert delivery, log/metrics retention, access controls, backup/restore drills, on-call ownership, capacity/load tests and rollback procedures. Keep evidence and a paper-autonomy review gate.
8. **Only after paper acceptance, design live adapter and data contracts.** Select a broker and licensed data source; implement the broker adapter in isolation, verify instrument/account/market semantics and idempotency in sandbox, then integrate reconciliation/readiness against real state.
9. **Require independent live authorization.** Add production identity/RBAC, credential lifecycle and network controls; run every live readiness check from measured system state; require separate approval and documented operational sign-off. Stage any live scope narrowly with monitoring, limits, kill-switch drills and rollback before considering broader autonomy.

## Evidence boundaries

- Source inspected includes core models, market/calendar, data provider/quality/resilience, strategy/regime/research/orchestration, risk/sizing, trading service, paper/live executor contracts, order lifecycle, recovery, persistence, learning, backtesting, API/bootstrap, dashboards, CI, Docker/Compose and operations documentation.
- The audit did not inspect secrets, call any external provider, use a broker account, start Docker, deploy services, or establish the status of a fresh GitHub Actions run.
- The 251-test result cited in prior session history was not rerun for this documentation-only audit. It is not evidence of live integration or unattended operational safety.
- Assessment is a point-in-time architectural review, not a profitability assessment, security certification, legal/data-license determination, or production-readiness approval.

FILES CHANGED:
docs/MASTER_ARCHITECTURE_COMPLIANCE.md
IMPLEMENTATION CHANGES:
NONE
