# Execution Boundary Inventory

## Purpose and invariant

This inventory identifies order-producing and portfolio-mutating entry points. The application remains paper-only in its configured bootstrap. Every supported platform order must enter through `TradingService`, receive a deterministic `RiskEngine` decision, and use `OrderManager` plus the simulated `PaperBroker`. Research scores, strategy selection, UI controls and proposal ranking are not risk approval.

Legacy dashboard state is separate from `PortfolioManager` and is not migrated or reconciled by this phase. Its order-mutating helpers fail closed; dashboard research and display code may still run.

## Entry-point classification

| Classification | Entry point | Behavior and boundary |
|---|---|---|
| `AUTHORITATIVE` | Authenticated `POST /paper/orders` in `src/stockmarket/api/app.py` | Validate the request, then delegate to the paper `TradingService`; `RiskEngine` evaluates it before the `OrderManager`/`PaperBroker` path. General manual paper orders remain explicit caller-quantity overrides. |
| `AUTHORITATIVE` | Authenticated `POST /intelligence/proposals/{proposal_id}/submit` in `src/stockmarket/api/app.py` | PAPER-only acceptance. Automatic sizing is the default; `MANUAL_OVERRIDE` is explicit and requires a positive quantity. A durable proposal claim is written before the service call. The route rejects stale proposals and never retries an ambiguous `SUBMITTING` state. |
| `AUTHORITATIVE` | `TradingService.submit()` and `TradingService.submit_signal()` in `src/stockmarket/core/trading_service.py` | Serialize service submissions within one process, build a fresh risk context, account for outstanding-order reservations, evaluate through `RiskEngine`, then submit through `OrderManager`. |
| `AUTHORITATIVE` | `TradingService.submit_sized_signal()` and `size_position()` in `src/stockmarket/core/trading_service.py` and `src/stockmarket/core/sizing.py` | Automatic proposal sizing uses a fresh quote, configured account risk/caps, portfolio exposure, available currency cash, FX conversion, instrument lot/minimum constraints and configured broker quantity/notional caps. The independently evaluated `RiskEngine` remains the final gate. Missing quote, FX, stop or reservation facts reject sizing. |
| `AUTHORITATIVE` | `OrderManager` and `PaperBroker` in `src/stockmarket/core/order_management.py` and `src/stockmarket/core/brokers.py` | Own paper order lifecycle and simulated execution. Persisted order transitions and recovery remain the order-state source of truth. |
| `LEGACY` | `_execute_paper_order()` in root `dashboard.py` | Raises before touching the dashboard-local holdings, cash or ledger. The former direct-mutation implementation has been removed. Its callers cannot place a legacy paper order. |
| `LEGACY` | `_record_trade()` in `dashboard_simple.py` | Raises before changing simple simulator holdings, shorts, cash, logs or trade files. Automatic and manual simulator callers share this boundary. |
| `RESEARCH ONLY` | `src/stockmarket/cli.py` commands (`backtest`, `signal`, `sweep`, `replay-best`, validation commands) | Analyze historical or market data and produce research/validation outputs; do not submit broker or paper orders. |
| `RESEARCH ONLY` | `src/stockmarket/webapp.py` and the remaining Streamlit research/dashboard rendering paths | Data display, signal ranking, backtesting and research. Any legacy dashboard order request reaches one of the disabled mutators above. |
| `TEST ONLY` | `tests/` fake gateways, paper broker fixtures and persistence/recovery tests | Exercise simulated order flow and rejection/recovery behavior; no test should submit a live broker order. |
| `UNAVAILABLE / MUST REMAIN DISABLED` | Authenticated `POST /live/orders` in `src/stockmarket/api/app.py` | Has a separate explicit live confirmation guard, but the default bootstrap does not construct a live service. The route is not an available execution path in the supported configuration. No live adapter is added by this phase. |

## Durability, idempotency and concurrency limits

- `proposal_submissions` records the complete proposal payload, proposal/generated timestamps, operator, sizing mode, client order ID and submission outcome. A unique proposal ID claim prevents a second submission. Replays return the existing recovered order; a `SUBMITTING` record without a confirmed order fails closed and requires reconciliation.
- Existing order repositories and recovery remain responsible for order state. The proposal record supplements, rather than replaces, order, risk, audit or fill persistence.
- `TradingService` serializes check-and-submit and counts active orders as exposure/cash reservations in the current process. It does **not** coordinate separate API processes or workers. Operate one API worker; multiple workers are unsupported until a durable cross-process reservation/locking design is implemented and tested.
- Proposal generation contexts are not persisted in a reconstructable form. After restart, an unsubmitted proposal must be researched again. Existing proposal IDs that already have a durable submission record cannot be replayed into a new order.
- Automatic sizing is configured in the API bootstrap from the existing `RiskSettings`, `MAX_ORDER_NOTIONAL`, `MAX_POSITION_QUANTITY`, portfolio balances/exposure and configured FX rates. FX conversion does not yet include source, spread or quote-time validation. Participation caps are not enabled unless validated average daily volume is made available.
- Failed persistence or uncertain broker/order state must remain visible and must not be translated into an empty portfolio, a fresh claim or an automatic retry.

## Ownership and maintenance

Keep order-entry behavior in `TradingService`; keep deterministic final decisions in `RiskEngine`; keep lifecycle in `OrderManager`; and keep paper execution in `PaperBroker`. Dashboard code may call the platform API in a future UI migration, but it must not regain a separate order mutation implementation. Extend this inventory when adding any new CLI, API, scheduled worker, dashboard control or broker adapter.
