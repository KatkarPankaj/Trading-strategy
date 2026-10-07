"""API-backed Streamlit dashboard for research, paper proposals and monitoring."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

import streamlit as st

from . import views
from .client import ApiClient, ApiError

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
    st.caption(
        "Research and order review use the platform API. Only explicit PAPER proposal "
        "acceptance is available here; arbitrary orders cannot be entered.")
    if st.sidebar.button("Refresh"):
        st.rerun()

    tabs = st.tabs(["Research (advisory)", "Opportunities", "Markets & regime",
                    "Signals", "Portfolio", "Orders", "PnL & drawdown",
                    "Strategies", "News & AI", "Risk", "System health", "Audit log"])
    renderers: list[Callable[[], None]] = [
        lambda: _research(client), lambda: _opportunities(client),
        lambda: _markets(client), lambda: _signals(client),
        lambda: _portfolio(client), lambda: _orders(client),
        lambda: _pnl(client), lambda: _strategies(client),
        lambda: _news(client), lambda: _risk(client),
        lambda: _health(health), lambda: _audit(client),
    ]
    for tab, render in zip(tabs, renderers):
        with tab:
            render()


def _research(client: ApiClient) -> None:
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
        result = client.post("/research", payload)
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


def _opportunities(client: ApiClient) -> None:
    st.subheader("Rank market opportunities")
    st.warning(
        "AI and research evidence are advisory. Proposals are not risk-approved. "
        "Acceptance is explicit, PAPER-only, and requires an operator-supplied quantity.")
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
                })
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
    st.subheader("Top signals")
    _table(views.top_signals(_safe(client, "/signals", limit=500) or []))


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
    st.caption("Paper order review. Proposal entries are submitted only after explicit acceptance and final RiskEngine approval.")
    only_open = st.checkbox("Open orders only")
    _table(views.frame(_safe(client, "/orders", open_only=only_open)))


def _pnl(client: ApiClient) -> None:
    st.subheader("PnL and drawdown")
    curves = views.pnl_curves(_safe(client, "/pnl") or [])
    if curves.empty:
        st.info("No PnL snapshots recorded yet.")
        return
    st.line_chart(curves[["equity"]])
    st.line_chart(curves[["drawdown"]])
    _table(curves.tail(50))


def _strategies(client: ApiClient) -> None:
    st.subheader("Strategy performance")
    _table(views.strategy_performance(
        _safe(client, "/trades", limit=1000) or []))
    perf = _safe(client, "/performance")
    if perf:
        st.json(perf["trades"])


def _news(client: ApiClient) -> None:
    st.subheader("News")
    _table(views.frame(_safe(client, "/news", limit=100)))
    st.subheader("AI explanations")
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


def _health(health: Any) -> None:
    st.subheader("System health")
    if not health:
        st.error("Health report unavailable.")
        return
    st.metric("Status", health["status"])
    _table(views.health_checks(health))
    st.json({k: health[k] for k in ("last_market_data_timestamp", "market_data_age_seconds",
                                    "last_successful_order", "error_counts")})


def _audit(client: ApiClient) -> None:
    st.subheader("Audit log")
    audit = _safe(client, "/audit")
    if not audit:
        return
    (st.success if audit["chain_intact"] else st.error)(
        "Audit chain intact" if audit["chain_intact"] else f"Audit chain problems: {audit['problems']}")
    _table(views.frame(audit["entries"]))
