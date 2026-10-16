# AI Trading Platform — Master Goal Summary

## 1. Our vision

Build an **AI-driven, international trading platform** that independently researches markets, discovers trading opportunities, selects suitable strategies based on evidence, and executes and manages trades in a controlled paper-trading environment before any consideration of real-money trading.

The AI should do more than summarize news or generate buy/sell signals. It should help the platform make evidence-based decisions about **what to trade, which strategy to use, when to enter, when to exit, and when not to trade.**

The goal is to build a reliable, continuously improving trading system—not just a dashboard or a collection of indicators.

## 2. The complete trading workflow

1. **Discover markets and instruments**  
   Scan supported exchanges, stocks, ETFs, and other supported instruments.

2. **Gather market evidence**  
   Collect price, volume, candles, news, events, liquidity, volatility, and market conditions.

3. **AI research and opportunity ranking**  
   Explain why an instrument may be interesting, assess evidence, and rank opportunities.

4. **Select an eligible strategy**  
   Choose from approved, validated strategies compatible with current conditions.

5. **Validate trade and risk**  
   Check entry, stop-loss, target, position size, capital, data freshness, and eligibility.

6. **Execute and manage paper trades**  
   Track orders, fills, positions, exits, P&L, and protective controls.

7. **Evaluate, learn, and improve**  
   Analyze outcomes, identify weaknesses, and improve research and strategy decisions through controlled validation.

## 3. Core capabilities

### A. Intelligent opportunity discovery

- Discover promising instruments beyond a fixed watchlist.
- Scan a broader supported market universe when existing candidates are unproductive.
- Use fresh market data, volume, liquidity, volatility, news, and strategy compatibility to screen candidates.
- Rank opportunities using measurable evidence, not arbitrary AI confidence scores.
- Explain why each candidate was added, retained, rejected, or deprioritized.
- Avoid duplicate candidates and excessive API calls.

**Important:** If every instrument is showing `HOLD` for approximately 30 minutes, that should trigger a fresh opportunity-discovery cycle—not force a trade or automatically discard existing instruments.

### B. AI-driven strategy selection

The AI should help determine which approved strategy best fits the opportunity and current market conditions.

Examples include:

- Opening Range Breakout (ORB)
- VWAP-based setups
- Momentum and volume breakout strategies
- Trend-following strategies
- Mean-reversion strategies
- News-driven opportunities, where supported by validated rules

The platform must evaluate strategy compatibility, historical evidence, costs, risk, and current conditions. It must not select a strategy merely because the AI recommends it.

### C. Trade planning and execution

For every proposed trade, the platform should determine and record:

- Instrument, exchange, and market
- Direction: long/buy or short/sell, where supported
- Strategy and strategy version
- Evidence and rationale
- Entry conditions and order type
- Stop-loss and target
- Position size and capital allocation
- Risk/reward and estimated transaction costs
- Expiry, invalidation, and exit conditions

Paper execution must correctly handle order submission, fills, partial fills where supported, rejected orders, open positions, exits, realized/unrealized P&L, and restart recovery.

### D. Continuous monitoring

The platform should keep monitoring positions and outstanding orders rather than stopping after generating a signal.

It should detect:

- Stop-loss or target conditions
- Stale market data and broken feeds
- Failed or rejected orders
- Missing protective exits
- Duplicate or conflicting workers
- Market-session changes and trading cutoffs
- Unexpected restarts or interrupted cycles

It should never open a new position simply to make the dashboard appear active.

### E. Evidence-based learning

Use recorded research decisions, strategy versions, signals, orders, fills, and trade outcomes to evaluate performance.

The system should identify which strategies and market conditions are working, which are failing, and why. Changes to strategy rules or eligibility should pass a defined validation and approval process before they can affect automatic trading.

**Learning must not mean uncontrolled self-modification or automatic promotion of unproven strategies.**

## 4. International trading is a core goal

The long-term architecture should support multiple markets, exchanges, currencies, trading calendars, broker integrations, and market-specific rules.

International support must be implemented and verified market by market. Support for one exchange does not mean the platform is ready for another.

Each market needs verified:

- Market-data availability and provider entitlements
- Trading hours, holidays, and special sessions
- Currency and account-cash handling
- Tick sizes, order rules, and short-selling restrictions
- Broker execution support and risk controls
- Market-specific strategy validation

A market that has not passed its readiness checks must remain unavailable for automatic execution.

## 5. Non-negotiable safety and reliability rules

1. **Validated strategies only.** Preserve the `NO_VALIDATED_STRATEGY` gate. If no eligible strategy has been approved, the platform may continue researching and ranking opportunities, but it must not create automatic entries that bypass this gate.
2. **Paper trading first.** No live orders or real-money execution as part of the current development goal.
3. **Fail closed.** Missing data, stale prices, invalid market readiness, failed risk checks, or unsupported order rules must block execution.
4. **Risk before opportunity.** No signal may bypass position sizing, capital limits, stop-loss rules, or other risk controls.
5. **No forced trades.** A rising market, prolonged `HOLD` status, or an empty trade history is not, by itself, a reason to buy or sell.
6. **Reliable state.** Prevent duplicate orders and overlapping cycles; preserve positions and pending orders across restarts.
7. **Traceability.** Every decision must be explainable from its market evidence, strategy version, risk checks, and execution history.
8. **No false readiness claims.** Do not report a feed, exchange, strategy, or broker as operational unless it has been verified.

## 6. Development priorities

### Priority 1 — Reliable trading foundation

Confirm market-data freshness, strategy eligibility, risk gates, order lifecycle, position management, protective exits, and restart safety.

### Priority 2 — Adaptive opportunity discovery

Expand beyond an unproductive watchlist, rank candidates efficiently, and use AI only where it adds meaningful value.

### Priority 3 — Evidence-based AI decisions

Improve research quality, strategy matching, decision explanations, and evaluation of historical outcomes.

### Priority 4 — Unified operator platform

Provide a clear API-backed interface for research, watchlists, strategy eligibility, paper orders, positions, P&L, risk, and operational health.

### Priority 5 — Verified multi-market expansion

Expand exchange, currency, and broker coverage only after each market's data, rules, calendar, and execution paths are verified.

## 7. AI efficiency and development workflow

To keep operating costs and development time under control:

- Reuse the existing AI client and configured model, currently `gpt-4.1-mini`, unless a measured evaluation justifies a change.
- Use deterministic screening before sending candidates to AI.
- Send only shortlisted candidates, with compact evidence and bounded prompts.
- Cache reusable research and avoid repeating identical requests.
- Apply request budgets, timeouts, concurrency limits, and rate-limit handling.
- Prefer incremental changes to the existing architecture; avoid unnecessary dependencies and broad rewrites.

### Copilot workflow

Ask Copilot to inspect relevant code, make targeted changes, and write/update appropriate tests, but explicitly forbid it from executing tests, builds, linters, type checks, migrations, or other validation commands. Copilot should return the exact commands for you to run manually. Paste the results back for review.

## 8. What success looks like

The project is progressing toward its goal when:

- New opportunities are discovered even when the current watchlist produces only `HOLD`.
- Research decisions use fresh, traceable evidence.
- Strategy selection is intelligent but constrained to approved, validated strategies.
- Valid paper trades can progress from opportunity to order, fill, position, and exit when all gates pass.
- Open positions and protective controls remain reliable through interruptions.
- Performance is measured from actual recorded outcomes, not AI-generated confidence alone.
- AI usage is cost-controlled and observable.
- Each supported market has independently verified readiness.
- The platform can explain both **why it traded and why it did not trade**.

## The master principle

> **Build an evidence-driven AI trading platform that discovers opportunities, selects validated strategies, manages risk, executes and monitors paper trades reliably, and improves through measured outcomes—while remaining cost-efficient, explainable, and safe.**

Every new feature, Copilot task, or architectural change should be evaluated against this goal. If it adds complexity without improving opportunity quality, decision correctness, execution reliability, risk management, or measurable learning, it should not be a priority.
