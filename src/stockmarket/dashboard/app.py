"""API-backed Streamlit dashboard for research, paper proposals and monitoring."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote
from uuid import uuid4

import streamlit as st

from . import views
from .client import ApiClient, ApiError
from ..core.api_contract import dashboard_compatibility_problem

_COLORS = {"paper": "#1f6feb", "live": "#d1242f", "unknown": "#6e7781"}


def _banner(label: str, level: str) -> None:
    st.markdown(
        f"<div style='background:{_COLORS[level]};color:white;padding:12px;border-radius:6px;"
        f"text-align:center;font-size:1.4rem;font-weight:700'>{label}</div>",
        unsafe_allow_html=True)


def _safe(client: ApiClient, path: str, **params: Any) -> Any:
    try:
        return client.get(path, **params)
    except ApiError as exc:
        st.warning(str(exc))
        return None


def _table(df: Any, empty: str = "No data yet.") -> None:
    if df is None or len(df) == 0:
        st.info(empty)
    else:
        st.dataframe(df, width="stretch")


def main() -> None:
    st.set_page_config(page_title="Trading Monitor", layout="wide")
    client = ApiClient.from_env()

    try:
        health = client.get("/health")
    except ApiError as exc:
        health = None
        st.error(str(exc))
    label, level = views.mode_banner(health)
    _banner(label, level)
    problem = dashboard_compatibility_problem(health)
    if problem is not None:
        st.error(problem)
        st.warning(
            "Interactive dashboard controls are unavailable until matching API/dashboard versions "
            "are running. This does NOT pause an already-running paper loop. Do not start another "
            "worker or bypass entry gates. Review schema compatibility and arrange an authorized "
            "controlled restart; this page does not restart services or migrate databases.")
        if health is not None:
            st.subheader("Read-only API health (compatibility unverified)")
            st.json(health)
        if st.sidebar.button("Refresh"):
            st.rerun()
        return
    personal = bool(health and health.get("application_mode") == "PERSONAL_RESEARCH")
    if personal:
        _banner("PERSONAL RESEARCH - RECOMMENDATION ONLY - EXECUTION DISABLED", "paper")
    st.caption(
        "Research uses the platform API. Human decision only; no orders are submitted."
        if personal else
        "Manual research is advisory. Manual proposal acceptance is PAPER-only; "
        "a separately enabled local paper loop can submit risk-checked simulated orders. "
        "Arbitrary orders cannot be entered.")
    if st.sidebar.button("Refresh"):
        st.rerun()
    page = st.sidebar.radio("Workspace", ["Trading Monitor", "Market Watch"])
    if page == "Market Watch":
        _market_watch(client)
        return

    activity = _paper_activity(client) if health and health.get("trading_mode") == "PAPER" and not personal else None
    if activity is not None:
        loop = activity.get("loop")
        if loop is not None:
            last = loop.get("last") or {}
            # MONITOR/EXITS_ONLY ticks carry no cycle status; show what the loop actually did.
            st.caption(
                f"Local paper loop: {loop['state']} | "
                f"Last tick: {last.get('action', 'none')}"
                f"{' (' + last['status'] + ')' if last.get('status') else ''}"
                f"{' at ' + last['at'] if last.get('at') else ''} | "
                "When enabled, the loop may submit simulated orders through deterministic strategy and risk checks.")
        diagnostics = _safe(client, "/paper/diagnostics") if loop and loop.get("state") != "DISABLED" else None
        if diagnostics is not None:
            st.caption(views.paper_diagnostics_line(diagnostics))
            if diagnostics.get("automatic_entries") == "BLOCKED_NO_VALIDATED_STRATEGY":
                st.warning(diagnostics["detail"])

    tabs = st.tabs(["Research (advisory)", "Opportunities", "Markets & regime",
                    "Signals", "Portfolio", "Orders", "PnL & drawdown",
                    "Strategies", "News & AI", "Risk", "System health", "Audit log"])
    renderers: list[Callable[[], None]] = [
        lambda: _personal_research(client) if personal else _research(client, activity),
        lambda: _personal_results(client) if personal else _opportunities(client, activity),
        lambda: _markets(client), lambda: _signals(client),
        lambda: _portfolio(client), lambda: _orders(client),
        lambda: _pnl(client), lambda: _strategies(client, activity),
        lambda: _news(client, activity), lambda: _risk(client),
        lambda: _health(health, activity, client=client), lambda: _audit(client),
    ]
    for tab, render in zip(tabs, renderers):
        with tab:
            render()


def _market_watch(client: ApiClient) -> None:
    st.header("Market Watch")
    st.caption(
        "Shared canonical registry and watchlist. Quotes are observations, not orders or risk approvals. "
        "Refresh is explicit; missing prices are never zero. No live execution.")
    profiles = _safe(client, "/market-watch/markets")
    if not profiles:
        st.warning("No enabled markets are available.")
        return
    market = st.selectbox("Watch market", [row["market"] for row in profiles],
                          key="watch_market")
    profile = next(row for row in profiles if row["market"] == market)
    readiness = _safe(client, "/paper/market-readiness")
    if readiness is not None:
        st.caption(f"Execution worker: {readiness['execution_market']} | PAPER | SINGLE_MARKET. "
                   "Watch selection does not switch execution.")
        if market == "IN":
            st.warning(f"India paper execution: {readiness['india_status']}. "
                       "Fresh observation quotes are not execution authorization.")
            st.json(readiness["india"])
    st.caption(
        f"{market} | {profile['currency']} | {profile['timezone']} | "
        f"Session: {profile['phase']} | Provider: {profile['provider']} | "
        f"Freshness limit: {profile['max_quote_age_seconds']} seconds")
    if not profile["calendar_covered"]:
        st.warning("Calendar coverage is unavailable. Trading sessions fail closed.")
    if profile["data_origin"] == "SYNTHETIC":
        st.warning("Synthetic mock provider: these prices are not real market observations.")
    else:
        st.caption("Provider provenance is displayed; real-origin and vendor vintages are not independently attested.")
    query = st.text_input("Search symbol, exchange identity or name", key="watch_query")
    result = _safe(client, "/market-watch/instruments", market=market, q=query, limit=50)
    if result and result["items"]:
        choices = {row["instrument_id"]: row for row in result["items"]}
        identity = st.selectbox("Canonical instrument", list(choices), key=f"watch_add_{market}")
        st.json(choices[identity])
        if result["next_cursor"]:
            st.caption("Showing the first 50 matches. Narrow the search to find another instrument.")
        if st.button("Add to shared watchlist", key="watch_add"):
            try:
                client.post("/watchlist", {"instrument_id": identity})
                st.success("Instrument added. No order submitted.")
            except ApiError as exc:
                st.error(str(exc))
    elif result is not None:
        st.info("No matching registered instruments. Import verified master metadata; quotes do not create instruments.")
    rows = _safe(client, "/watchlist", market=market)
    if rows:
        _table(rows)
        identity = st.selectbox("Watched instrument to remove",
                                [row["instrument_id"] for row in rows], key=f"watch_remove_{market}")
        if st.button("Remove from shared watchlist", key="watch_remove"):
            try:
                client.post("/watchlist/remove", {"instrument_id": identity})
                st.rerun()
            except ApiError as exc:
                st.error(str(exc))
        if st.button("Refresh market quotes", key="watch_refresh"):
            try:
                refresh = client.post(
                    f"/market-watch/refresh?market={quote(market, safe='')}", {}, timeout=30)
                _table(refresh["items"])
                if refresh["incomplete"]:
                    st.warning("Refresh was incomplete; unchecked observations retain their original timestamps.")
            except ApiError as exc:
                st.error(str(exc))
    elif rows is not None:
        st.info("No instruments watched in this market.")


def _paper_activity(client: ApiClient) -> dict[str, Any]:
    loop = _safe(client, "/paper/loop")
    activity: dict[str, Any] = {"loop": loop, "cycles": [], "cycle": None, "research": None}
    if loop is None or loop.get("state") == "DISABLED":
        return activity
    cycles = _safe(client, "/paper/cycles", limit=10)
    if cycles is None:
        return activity
    activity["cycles"] = cycles
    if not cycles:
        return activity
    selected = st.sidebar.selectbox(
        "Paper cycle to inspect", [row["run_id"] for row in cycles],
        index=next((index for index, row in enumerate(cycles) if row["status"] == "COMPLETE"), 0),
        format_func=lambda key: next(
            f"{row['as_of']} | {row['status']}" for row in cycles if row["run_id"] == key),
    )
    cycle = _safe(client, f"/paper/cycles/{quote(selected, safe='')}")
    activity["cycle"] = cycle
    if cycle is not None:
        research_id = cycle["payload"].get("research_run_id")
        if research_id:
            activity["research"] = _safe(
                client, f"/research/autonomous/{quote(research_id, safe='')}")
    return activity


def _cycle_outcomes(activity: dict[str, Any]) -> None:
    st.subheader("Persisted paper-cycle outcomes")
    st.caption(
        "COMPLETE means cycle processing finished, not that a trade was placed. "
        "NO_TRADE and NON_ACTIONABLE are expected when strategy conditions are not met.")
    cycle = activity.get("cycle")
    if cycle is None:
        st.info("No paper cycle selected or available.")
        return
    st.caption(f"Cycle: {cycle['run_id']} | Status: {cycle['status']} | As-of: {cycle['as_of']}")
    if cycle["payload"].get("failure"):
        st.error(cycle["payload"]["failure"])
    _table(
        views.cycle_candidates(cycle.get("candidates", [])),
        "Cycle is still processing. Refresh for updates or select a completed cycle."
        if cycle["status"] in {"RUNNING", "RECOVERING"}
        else "No candidates recorded for this cycle.")
    with st.expander("Cycle events and position-management results"):
        st.json({"payload": cycle["payload"], "events": cycle.get("events", [])})
    with st.expander("Candidate decision traces (not order records)"):
        st.json([{"candidate_id": item["candidate_id"],
                  "decision_trace": item["payload"].get("decision_trace"),
                  "trace_status": "RECORDED" if item["payload"].get("decision_trace")
                  else "LEGACY_TRACE_UNAVAILABLE"}
                 for item in cycle.get("candidates", [])])


def _personal_research(client: ApiClient) -> None:
    st.subheader("Personal market research")
    st.warning(
        "RECOMMENDATION ONLY. EXECUTION: NOT SUBMITTED. RISK: NOT FINAL EXECUTION AUTHORITY. "
        "AI confidence is not a probability of profit. Missing AI withholds final BUY/SHORT recommendations.")
    status = _safe(client, "/research/personal/status")
    if status is None:
        return
    initial = _safe(client, "/markets/status")
    if initial is None:
        return
    labels = {"AUTO": "Auto", **{profile["market"]: profile["label"] for profile in initial["profiles"]}}
    selection = st.selectbox(
        "Market", list(labels), format_func=labels.get, key="personal_market_selection")
    context = initial if selection == "AUTO" else _safe(
        client, "/markets/status", selected_market=selection)
    if context is None:
        return
    resolved = context["resolved_market"]
    st.subheader(f"Resolved market: {context['label']}")
    st.caption(
        f"Exchange: {context['exchange']} | Currency: {context['currency']} | "
        f"Timezone: {context['timezone']} | Session: {context['session']} | Status: {context['status']}")
    st.caption(
        f"Exchange-local time: {context['local_timestamp']} | "
        f"Provider: {context['data_provider']} | Resolution: {context['resolution_reason']}")
    if context["ai_status"] == "CONFIGURED":
        st.success("AI: Configured (advisory assessment only)")
    else:
        st.warning("AI: Not configured - deterministic research still available; final BUY/SHORT withheld.")
    for limitation in context["limitations"]:
        st.warning(limitation)
    with st.expander("Market profiles, sessions and universe discovery"):
        st.json({"selected_market": selection, "context": context, "discovery": status["discovery"]})
    options = {item["universe_id"]: item for item in context["universes"]}
    if not options:
        st.error("No single-market research universe is configured for this market.")
        return
    universe_keys = list(options)
    selected_universe = st.selectbox(
        "Research universe", universe_keys,
        index=universe_keys.index(context["default_universe_id"]),
        format_func=lambda key: options[key]["name"], key=f"personal_universe_{resolved}")
    st.caption(
        f"Registered: {options[selected_universe]['instrument_count']} | "
        f"Active/tradable metadata: {options[selected_universe]['eligible_metadata_count']}. "
        "Registration is not proof of data availability or a trade setup.")
    instruments = _safe(client, "/instruments", market=resolved)
    if instruments is None:
        return
    if instruments:
        selected_instrument = st.selectbox(
            "Instrument diagnostics", [item["instrument_id"] for item in instruments],
            key=f"personal_diagnostics_{resolved}")
        if st.button("Check market data and session"):
            diagnostics = _safe(client, f"/research/personal/diagnostics/{quote(selected_instrument, safe='')}")
            if diagnostics is not None:
                st.json(diagnostics)
    strategies = context["supported_strategies"]
    if not strategies:
        st.error("No deterministic strategy is registered.")
        return
    with st.form(f"personal_research_{resolved}"):
        strategy = st.selectbox("Registered deterministic strategy", strategies)
        top_n = st.number_input("Maximum candidates", min_value=1, max_value=10, value=5, step=1)
        st.caption("Decision timestamp defaults to the last five-minute boundary. Future or incomplete bars are excluded.")
        submitted = st.form_submit_button("Scan & Research")
    if submitted:
        try:
            result = client.post("/research/personal/runs", {
                "universe_id": selected_universe, "strategy_name": strategy,
                "top_n": int(top_n), "idempotency_key": uuid4().hex,
                "selected_market": selection,
            }, timeout=300)
        except ApiError as exc:
            st.error(str(exc))
        else:
            st.session_state["personal_research_result"] = result
    result = st.session_state.get("personal_research_result")
    if result and result.get("market_context", {}).get("resolved_market") == resolved:
        _recommendations(result)


def _recommendations(result: dict[str, Any]) -> None:
    if result.get("market_context"):
        context = result["market_context"]
        st.caption(
            f"Run market: {context['label']} | Selected: {context['selected_market']} | "
            f"{context['session']} | {context['currency']} | {context['timezone']}")
    st.caption(
        f"Run: {result['run_id']} | Status: {result['status']} | "
        f"As-of: {result['as_of']} | EXECUTION: {result['execution']}")
    if result.get("failure"):
        st.error(result["failure"])
    rows = result.get("recommendations", [])
    _table(views.frame([{
        "Rank": row["rank"], "Instrument": row["instrument_id"],
        "Direction": row["direction"], "Score": row["score"],
        "Strategy": row["strategy"], "Data": row["data_quality"],
        "AI": row["ai_status"], "Risk flags": ", ".join(row["risk_flags"]),
        "Reason": row["reason"],
    } for row in rows]), "No eligible recommendations. See scanner diagnostics below.")
    for row in rows:
        with st.expander(f"{row['instrument_id']} - signal, prices, AI, regime and evidence"):
            st.json(row)
    with st.expander("Scanner diagnostics", expanded=not rows):
        _table(views.frame([{
            "Instrument": item["instrument_id"],
            "Data": item.get("ohlcv_status", item.get("quality_status")),
            "Provider": item.get("provider_status", item.get("payload", {}).get("provider_status")),
            "Calendar covered": item.get("calendar_covered", False),
            "Reasons": ", ".join(item.get("reasons", item.get("payload", {}).get("rejection_reasons", []))),
        } for item in result.get("diagnostics", [])]))
    with st.expander("Full persisted provenance"):
        st.json(result)


def _personal_results(client: ApiClient) -> None:
    st.subheader("Persisted personal recommendations")
    runs = _safe(client, "/research/personal/runs", limit=10)
    if not runs:
        st.info("Run personal research to persist recommendations and rejected-case diagnostics.")
        return
    selected = st.selectbox("Research run", [run["run_id"] for run in runs])
    _recommendations(next(run for run in runs if run["run_id"] == selected))


def _research(client: ApiClient, activity: dict[str, Any] | None = None) -> None:
    if activity is not None:
        st.subheader("Automatic paper research history")
        _table(views.frame([
            {key: row.get(key) for key in ("run_id", "as_of", "status", "stage", "updated_at")}
            for row in activity["cycles"]
        ]), "No automatic paper cycles recorded yet.")
        research = activity.get("research")
        if research is not None:
            with st.expander("Selected cycle's persisted research and strategy context"):
                st.json(research)
    st.subheader("Run market research")
    st.warning(
        "This research form is advisory and never submits orders. AI rankings are advisory, "
        "and model-reported confidence is not calibrated.")
    instruments = _safe(client, "/instruments")
    if instruments is None:
        return
    if not instruments:
        st.info("No instruments are available from the API.")
        return
    options = {
        f"{item['symbol']} ({item['market']} · {item['instrument_id']})":
        item["instrument_id"]
        for item in instruments
    }
    labels = list(options)
    with st.form("market_research"):
        selected = st.selectbox("Instrument", labels)
        st.caption(
            "Optional operator-supplied scores must be based on evidence you have verified. "
            "They are not fetched from a sector/fundamental vendor.")
        components = st.multiselect(
            "Include research components",
            ("volume", "momentum", "sector", "news", "fundamental"),
            default=[],
        )
        scores = {
            component: st.slider(
                f"{component.title()} score (-1 bearish to +1 bullish)",
                min_value=-1.0,
                max_value=1.0,
                value=0.0,
                step=0.05,
                key=f"research_score_{component}",
            )
            for component in components
        }
        submitted = st.form_submit_button("Run research")
    if not submitted:
        return
    observed_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "instrument_id": options[selected],
        "as_of": observed_at,
        "evidence": [
            {
                "component": component,
                "score": score,
                "observed_at": observed_at,
                "source": "dashboard_operator_input",
            }
            for component, score in scores.items()
        ],
    }
    try:
        result = client.post("/research", payload, timeout=300)
    except ApiError as exc:
        st.error(str(exc))
        return
    st.caption(f"Status: {result['status']} · Mode: {result['trading_mode']}")
    if result.get("reason"):
        st.warning(result["reason"])
    for warning in result.get("research_warnings", ()):
        st.warning(warning)
    decision = result.get("decision")
    if decision is not None:
        st.metric(
            "Deterministic aggregate",
            decision["action"],
            f"Confidence: {decision['confidence']:.1f}% (deterministic score, not a forecast)",
        )
        st.write("Reason codes:", ", ".join(decision["reason_codes"]) or "None")
    selection = result.get("selection")
    if selection is not None:
        st.subheader("AI strategy ranking (advisory)")
        st.write(selection["summary"])
        st.dataframe(views.frame(selection["ranked_strategies"]), width="stretch")
        if selection.get("risks"):
            st.write("AI-listed risks:", "; ".join(selection["risks"]))
    signal = result.get("signal")
    if signal is not None:
        st.subheader("Deterministic strategy output")
        st.json(signal)
    if decision is not None:
        with st.expander("Regime and research evidence"):
            st.json({
                "regime": result.get("regime"),
                "research_evidence": result.get("research_evidence"),
                "decision_explanation": decision.get("explanation"),
            })


def _opportunities(client: ApiClient, activity: dict[str, Any] | None = None) -> None:
    if activity is not None:
        _cycle_outcomes(activity)
        if activity.get("research") is not None:
            st.subheader("Advisory candidates from the selected cycle")
            _table(views.research_candidates(activity["research"].get("candidates", [])))
    st.subheader("Rank market opportunities")
    st.warning(
        "AI and research evidence are advisory. Proposals are not risk-approved. "
        "Acceptance is explicit and PAPER-only; sizing and risk checks remain deterministic.")
    instruments = _safe(client, "/instruments")
    if instruments is None:
        return
    if not instruments:
        st.info("No instruments are available from the API.")
        return

    options = {
        f"{item['symbol']} ({item['market']} · {item['instrument_id']})":
        item["instrument_id"]
        for item in instruments
    }
    labels = list(options)
    with st.form("market_opportunities"):
        selected = st.multiselect("Candidate instruments (up to 10)", labels)
        submitted = st.form_submit_button("Rank opportunities")
    if submitted:
        st.session_state.pop("market_intelligence_result", None)
        if not selected:
            st.error("Select at least one instrument.")
        elif len(selected) > 10:
            st.error("Select no more than 10 instruments.")
        else:
            as_of = datetime.now(timezone.utc).isoformat()
            try:
                result = client.post("/intelligence/opportunities", {
                    "instrument_ids": [options[label] for label in selected],
                    "as_of": as_of,
                }, timeout=300)
            except ApiError as exc:
                st.error(str(exc))
            else:
                st.session_state["market_intelligence_result"] = result

    result = st.session_state.get("market_intelligence_result")
    if not result:
        return
    st.caption(
        f"Mode: {result.get('trading_mode', 'UNKNOWN')} · "
        f"as_of: {result.get('as_of', 'unknown')} · "
        f"execution: {result.get('execution', 'UNKNOWN')}")
    proposals = result.get("proposals") or []
    if not proposals:
        st.info("No actionable proposals were produced for this run.")
    else:
        st.dataframe(views.frame([
            {
                "rank": item["rank"],
                "symbol": item["symbol"],
                "market": item["market"],
                "side": item["side"],
                "intent": item.get("intent", "UNSPECIFIED"),
                "strategy": item["strategy"],
                "opportunity_score": item["opportunity_score"],
                "aggregate_score": item["aggregate_score"],
                "entry": item["entry_price"],
                "stop": item["stop_loss"],
                "target": item["take_profit"],
                "as_of": item["as_of"],
            }
            for item in proposals
        ]), width="stretch")
        for item in proposals:
            with st.expander(
                f"Rank {item['rank']}: {item['symbol']} {item['side']} "
                f"via {item['strategy']}"):
                st.json({
                    "proposal_id": item["proposal_id"],
                    "regime": item["regime"],
                    "strategy_selection": item["strategy_selection"],
                    "research_evidence": item["research_evidence"],
                    "research_warnings": item["research_warnings"],
                    "explanation": item["explanation"],
                    "risk_status": item["risk_status"],
                })
                with st.form(f"submit_proposal_{item['proposal_id']}"):
                    operator = st.text_input(
                        "Operator", key=f"proposal_operator_{item['proposal_id']}")
                    quantity = st.number_input(
                        "Paper order quantity",
                        min_value=1,
                        max_value=1_000_000_000,
                        value=1,
                        step=1,
                        key=f"proposal_quantity_{item['proposal_id']}",
                    )
                    accept = st.form_submit_button("Submit proposal to PAPER risk gate")
                if accept:
                    if not operator.strip():
                        st.error("Enter the operator name for the audit record.")
                        continue
                    try:
                        health = client.get("/health")
                        if health.get("trading_mode") != "PAPER":
                            st.error("Proposal submission is disabled unless the API confirms PAPER mode.")
                            continue
                        submission = client.post(
                            "/intelligence/proposals/"
                            f"{quote(item['proposal_id'], safe='')}/submit",
                            {"operator": operator.strip(), "quantity": int(quantity)},
                        )
                    except ApiError as exc:
                        st.error(str(exc))
                        continue
                    risk = submission.get("risk_decision") or {}
                    order = submission.get("order") or {}
                    if submission.get("duplicate"):
                        st.info(
                            f"Duplicate acceptance; no new order was sent. "
                            f"Existing paper order status: {order.get('status', 'UNKNOWN')}.")
                    elif risk.get("status") == "APPROVED":
                        st.success(
                            f"Risk approved; paper order status: "
                            f"{order.get('status', 'UNKNOWN')}.")
                    else:
                        st.warning(
                            f"Risk decision: {risk.get('status', 'UNKNOWN')} — "
                            f"{risk.get('reason', 'No reason returned')}")
                    st.json(submission)

    assessments = result.get("assessments") or []
    if assessments:
        with st.expander("Candidate assessments"):
            st.dataframe(views.frame(assessments), width="stretch")


def _markets(client: ApiClient) -> None:
    st.subheader("Global market status")
    data = _safe(client, "/markets")
    _table(views.frame(data["markets"]) if data else None)
    st.subheader("Market regime")
    signals = _safe(client, "/signals", limit=500)
    st.caption(
        "Derived from the regime recorded on recent signals; a dedicated regime engine is not wired in yet.")
    _table(views.regime_counts(signals or []))


def _signals(client: ApiClient) -> None:
    st.subheader("Deterministic signal evaluations (not orders)")
    st.caption("HOLD / NO_TRADE records are abstentions, not actionable BUY/SHORT signals. "
               "A signal is not risk approval or an order; consult actual paper orders and fills.")
    risk = _safe(client, "/risk", limit=1000) or {}
    _table(views.top_signals(_safe(client, "/signals", limit=500) or [],
                             decisions=risk.get("recent_decisions")))


def _portfolio(client: ApiClient) -> None:
    st.subheader("Portfolio")
    p = _safe(client, "/portfolio")
    if p:
        c = st.columns(4)
        c[0].metric(f"Equity ({p['base_currency']})", f"{p['equity']:,.2f}")
        c[1].metric("Daily PnL", f"{p['daily_pnl']:,.2f}")
        c[2].metric("Drawdown", f"{p['drawdown']:.2%}")
        c[3].metric("Gross exposure", f"{p['gross_exposure']:,.2f}")
        st.json({k: p[k] for k in ("cash", "sector_exposure",
                "currency_exposure", "fees", "slippage")})
    st.subheader("Open positions")
    pos = _safe(client, "/positions")
    _table(views.frame(pos["positions"])
           if pos else None, "No open positions.")


def _orders(client: ApiClient) -> None:
    st.subheader("Orders")
    st.caption(
        "Paper order review. Manual entries require explicit proposal acceptance; "
        "the enabled local paper loop may submit automatically. Both paths require final RiskEngine approval.")
    only_open = st.checkbox("Open orders only")
    _table(views.frame(_safe(client, "/orders", open_only=only_open)))
    st.subheader("Actual simulated paper fills")
    st.caption("An accepted order is not a fill. Fees, slippage and realized PnL below come from execution.")
    fills = _safe(client, "/paper/fills", limit=100)
    if fills is not None:
        _table(views.frame(fills["fills"]), "No simulated fills recorded.")
    else:
        st.error("Paper fill history unavailable. This is not evidence of an empty fill ledger. "
                 "If the API returned 404, verify the API version/base URL and refresh after "
                 "an operator-controlled API restart.")


def _pnl(client: ApiClient) -> None:
    st.subheader("PnL and drawdown")
    curves = views.pnl_curves(_safe(client, "/pnl") or [])
    if curves.empty:
        st.info("No PnL snapshots recorded yet.")
        return
    st.line_chart(curves[["equity"]])
    st.line_chart(curves[["drawdown"]])
    _table(curves.tail(50))


def _strategies(client: ApiClient, activity: dict[str, Any] | None = None) -> None:
    if activity is not None and activity.get("research") is not None:
        st.subheader("Strategies configured for the selected research run")
        request = activity["research"]["payload"].get("request", {})
        _table(views.frame(request.get("strategy_catalog", [])))
        st.caption("Registration and AI selection do not establish profitability or authorize an order.")
        _table(views.research_candidates(activity["research"].get("candidates", [])))
    st.subheader("Completed paper trades and strategy feedback")
    st.caption("Gross price PnL minus fees = net PnL. Fill prices already include slippage. "
               "Samples are grouped by version, parameters, timeframe, regime and currency; "
               "paper performance is not out-of-sample validation or risk approval.")
    outcomes = _safe(client, "/paper/outcomes", limit=100)
    _table(views.frame(outcomes["outcomes"]) if outcomes else None,
           "No fully closed paper position episodes recorded.")
    feedback = _safe(client, "/paper/strategy-performance")
    _table(views.frame(feedback["performance"]) if feedback else None,
           "No closed paper sample available.")
    st.subheader("AI strategy lifecycle - PAPER only")
    st.caption("Versioned trusted-template proposals and cost-aware historical evaluations. "
               "Historical folds are exploratory. Post-proposal holdouts must be registered "
               "before observation and keep their original window and policy. "
               "Activation needs qualified evidence and explicit authorized approval. "
               "Monitoring never invokes AI, runs evaluations or creates orders.")
    laboratory = _safe(client, "/strategy-lab/proposals", limit=20)
    if laboratory and laboratory["proposals"]:
        _table(views.frame([
            {"strategy_version": p["strategy_version_id"],
             "template": p["specification"]["template"], "timeframe": p["specification"]["timeframe"],
             "state": p["lifecycle"]["state"], "configuration": p["specification"]["parameters"],
             "eligibility_failures": p.get("eligibility", {}).get("failures", []),
             "eligible_for_review": p.get("eligibility", {}).get("eligible_for_review", False)}
            for p in laboratory["proposals"]]))
        st.json(laboratory["proposals"])
        with st.expander("Explicit authorized paper lifecycle action"):
            with st.form("paper_strategy_transition"):
                selected = st.selectbox("Strategy version", laboratory["proposals"],
                                        format_func=lambda p: p["strategy_version_id"])
                target = st.selectbox("Requested state", [
                    "APPROVED_FOR_PAPER", "ACTIVE_FOR_PAPER", "PAUSED", "RETIRED"])
                operator = st.text_input("Operator")
                reason = st.text_area("Review / transition reason")
                acknowledged = st.checkbox("I reviewed the evidence and acknowledge multi-proposal selection bias")
                submitted = st.form_submit_button("Request PAPER state transition")
            if submitted:
                if not operator.strip() or not reason.strip():
                    st.error("Operator and review reason are required.")
                else:
                    try:
                        st.json(client.post(
                            f"/strategy-lab/proposals/{selected['proposal_id']}/transition",
                            {"target": target, "operator": operator.strip(), "reason": reason.strip(),
                             "request_id": uuid4().hex, "acknowledge_selection_bias": acknowledged}))
                    except ApiError as exc:
                        st.error(str(exc))
    else:
        st.info("No strategy proposals recorded.")
    st.subheader("Strategy ranking changes and forward paper measurements")
    ranking = _safe(client, "/paper/strategy-rankings")
    if ranking:
        st.caption("Observational forward results, not causal proof of improvement or profitability.")
        st.caption("Evidence v2 compares exact instrument/version/timeframe outcomes with at least "
                   "30 trades across 10 observation dates and three chronological blocks. "
                   "Unknown regimes are not regime-conditioned evidence. Final holdouts are review-only, "
                   "never selection features. Insufficient evidence preserves the AI preference; "
                   "all compared qualified scores below NO_TRADE cause abstention.")
        _table(views.frame([
            {"assessment": record["assessment_id"], "strategy": item["strategy"],
             "source": item["source"], "sample": item["sample_size"],
             "status": item.get("metrics", {}).get("sample_status", "LEGACY_POLICY"),
             "score": item["conservative_net_return"],
             "profit_factor": item.get("metrics", {}).get("profit_factor"),
             "window_instability": item.get("metrics", {}).get("window_instability"),
             "recommendation": record.get("recommendation", "LEGACY_POLICY")}
            for record in ranking.get("rankings", []) for item in record["evidence"]]))
        st.json(ranking)
    st.subheader("Legacy/backtest strategy performance (separate from paper feedback)")
    _table(views.strategy_performance(
        _safe(client, "/trades", limit=1000) or []))
    perf = _safe(client, "/performance")
    if perf:
        st.json(perf["trades"])
    _prospective_evidence(client)


def _prospective_evidence(client: ApiClient) -> None:
    st.subheader("Prospective evidence and supervised-model readiness")
    st.caption("Read-only persisted observations. No model is trained or promoted here; "
               "fixture data and insufficient samples do not establish predictive improvement.")
    readiness = _safe(client, "/intelligence/prospective/readiness")
    if readiness is not None:
        st.warning(f"Model readiness: {readiness['status']} | {readiness['mode']}")
        _table(views.frame([
            {"feature": name, "missing_decisions": count}
            for name, count in readiness["missing_feature_counts"].items()
        ]), "No missing feature values reported; source/sample checks still apply.")
        st.json(readiness)
    ledger = _safe(client, "/intelligence/prospective/decisions", limit=20)
    if ledger is not None:
        _table(views.frame([
            {key: decision.get(key) for key in (
                "decision_id", "decision_at", "instrument_id", "selected_strategy",
                "timeframe", "feature_schema", "ranking_policy", "source_verification")}
            for decision in ledger["decisions"]
        ]), "No prospective decisions captured.")
        if ledger["decisions"]:
            with st.expander("Frozen decision and collected execution/outcome evidence"):
                selected = st.selectbox(
                    "Prospective decision", ledger["decisions"],
                    format_func=lambda decision: decision["decision_id"])
                observation = _safe(
                    client, f"/intelligence/prospective/decisions/{quote(selected['decision_id'], safe='')}")
                if observation is not None:
                    st.json(observation)


def _news(client: ApiClient, activity: dict[str, Any] | None = None) -> None:
    if activity is not None and activity.get("research") is not None:
        st.subheader("AI assessments used by the selected paper cycle")
        st.caption("Persisted advisory assessments, not risk approvals. Confidence is not calibrated probability.")
        for candidate in activity["research"].get("candidates", []):
            with st.expander(f"Research evidence: {candidate['instrument_id']}"):
                st.json(candidate.get("opportunity") or {
                    "stage": candidate.get("stage"), "error": candidate.get("error")})
                snapshot_id = candidate.get("snapshot_id")
                if snapshot_id:
                    evidence = _safe(client, f"/research/snapshots/{quote(snapshot_id, safe='')}")
                    if evidence is not None:
                        st.json(evidence)
    st.subheader("Stored standalone news events")
    st.caption(
        "These tables contain standalone news research records. Automatic-cycle news "
        "and AI evidence are shown above; older standalone events are not backfilled.")
    _table(views.frame(_safe(client, "/news", limit=100)))
    st.subheader("Stored standalone news analyses")
    st.caption("AI output is research input only; it never places orders.")
    _table(views.frame(_safe(client, "/ai-analyses", limit=100)))


def _risk(client: ApiClient) -> None:
    st.subheader("Risk status")
    risk = _safe(client, "/risk")
    if not risk:
        return
    st.json({"limits": risk["limits"], "current": risk["current"]})
    if risk["disabled_controls"]:
        st.warning(f"Disabled risk controls: {risk['disabled_controls']}")
    st.subheader("Rejections by reason")
    _table(views.rejection_summary(
        risk["recent_decisions"]), "No rejections recorded.")
    st.subheader("Recent risk decisions")
    _table(views.frame(risk["recent_decisions"]))


def _health(health: Any, activity: dict[str, Any] | None = None,
            *, client: ApiClient | None = None) -> None:
    st.subheader("System health")
    loop = (activity or {}).get("loop")
    if loop is not None:
        st.subheader("Paper loop and startup recovery")
        st.json(loop)
        protective = loop.get("protective_monitor")
        if protective is not None:
            st.subheader("Independent protective monitoring")
            st.json(protective)
            st.caption("Latency budget is operational telemetry, not a hard real-time guarantee. "
                       "A stalled provider or execution/database lock can delay protective exits.")
            if protective.get("enabled") and not protective.get("entry_ready"):
                st.error("Protective monitoring/recovery is not ready; new entries are blocked.")
        if loop.get("state") == "STOPPED_AFTER_FAILURES":
            st.error("Automatic entries halted after repeated failures; protective exits remain enabled. "
                     "Resolve failures before restarting the API.")
        for failure in loop.get("recovery_failures", []):
            st.error(f"Cycle {failure['run_id']}: {failure['error']}")
        if client is not None and loop.get("state") != "DISABLED":
            with st.form("paper_loop_control"):
                st.caption("Pause/resume automatic entries only. Stop/target monitoring and "
                           "end-of-day exits remain enabled. Resuming never clears a kill switch.")
                enabled = st.checkbox("Allow automatic PAPER entries",
                                      value=loop.get("operator_entries_enabled", True))
                operator = st.text_input("Paper session operator")
                reason = st.text_input("Paper session control reason")
                submitted = st.form_submit_button("Apply automatic entry control")
            if submitted:
                if not operator.strip() or not reason.strip():
                    st.error("Operator and reason are required.")
                else:
                    try:
                        result = client.post("/paper/loop/control", {
                            "entries_enabled": enabled, "operator": operator.strip(), "reason": reason.strip()})
                    except ApiError as exc:
                        st.error(str(exc))
                    else:
                        st.json(result)
                        st.rerun()
    if not health:
        st.error("Health report unavailable.")
        return
    st.metric("Status", health["status"])
    if health.get("paper_readiness") is not None:
        st.subheader("Paper execution readiness (not risk approval)")
        st.json(health["paper_readiness"])
    if health.get("execution_data_status") is not None:
        st.subheader("Execution data and selected market session")
        st.json(health["execution_data_status"])
    _table(views.health_checks(health))
    st.json({k: health[k] for k in ("last_market_data_timestamp", "market_data_age_seconds",
                                    "last_successful_order", "error_counts")})
    st.caption(
        "Market-data health tracks validated quotes and freshness-required bars, not all research reads. "
        "Historical research activity does not prove a current executable price. "
        "Missing or stale execution data still blocks new entries.")
    st.json({
        "last_research_data_timestamp": health.get("last_research_data_timestamp"),
        "last_research_data_received_at": health.get("last_research_data_received_at"),
    })


def _audit(client: ApiClient) -> None:
    st.subheader("Audit log")
    audit = _safe(client, "/audit")
    if not audit:
        return
    (st.success if audit["chain_intact"] else st.error)(
        "Audit chain intact" if audit["chain_intact"] else f"Audit chain problems: {audit['problems']}")
    _table(views.frame(audit["entries"]))
