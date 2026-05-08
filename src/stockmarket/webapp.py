from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import streamlit as st

from stockmarket.backtest import run_backtest
from stockmarket.config import TradingConfig
from stockmarket.data import fetch_intraday_data
from stockmarket.strategy import add_strategy_columns
from stockmarket.sweep import run_parameter_sweep


st.set_page_config(page_title="StockMarket Intraday App", layout="wide")
st.title("StockMarket Intraday App (NSE/BSE)")
st.caption("Personal-use intraday research UI")


@st.cache_data(ttl=300)
def _load_data(symbol: str, interval: str, period: str, tz: str) -> pd.DataFrame:
    return fetch_intraday_data(symbol, interval, period, tz=tz)


def _save_csv(df: pd.DataFrame, prefix: str, symbol: str) -> Path:
    out_dir = Path("outputs")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{prefix}_{symbol.replace('.', '_')}.csv"
    df.to_csv(out_file, index=False)
    return out_file


config_path = st.sidebar.text_input("Config path", value="config.json")

try:
    cfg = TradingConfig.from_json(config_path)
except Exception as exc:
    st.error(f"Failed to load config: {exc}")
    st.stop()

st.sidebar.subheader("Runtime overrides")
symbol = st.sidebar.text_input("Symbol", value=cfg.symbol)
interval = st.sidebar.text_input("Interval", value=cfg.interval)
period = st.sidebar.text_input("Period", value=cfg.period)
market_timezone = st.sidebar.text_input(
    "Market timezone", value=cfg.market_timezone)

cfg = replace(cfg, symbol=symbol, interval=interval,
              period=period, market_timezone=market_timezone)

col1, col2, col3 = st.columns(3)
run_backtest_btn = col1.button("Run Backtest", width='stretch')
show_signals_btn = col2.button("Show Signals", width='stretch')
run_sweep_btn = col3.button("Run Sweep", width='stretch')

st.divider()

if run_backtest_btn:
    try:
        with st.spinner("Fetching data and running backtest..."):
            df = _load_data(cfg.symbol, cfg.interval,
                            cfg.period, cfg.market_timezone)
            result = run_backtest(df, cfg)

        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("Trades", int(result.summary["total_trades"]))
        m2.metric("Win Rate", f"{result.summary['win_rate']:.2%}")
        m3.metric("Net PnL", f"{result.summary['net_pnl']:.2f}")
        m4.metric("Return", f"{result.summary['return_pct']:.2%}")
        m5.metric("Max DD", f"{result.summary['max_drawdown_pct']:.2%}")
        m6.metric("Profit Factor", f"{result.summary['profit_factor']:.2f}")

        if result.trades.empty:
            st.info("No trades generated for this config and period.")
        else:
            out_file = _save_csv(result.trades, "web_trades", cfg.symbol)
            st.success(f"Trades saved to {out_file}")
            st.dataframe(result.trades, width='stretch')

    except Exception as exc:
        st.error(f"Backtest failed: {exc}")

if show_signals_btn:
    try:
        with st.spinner("Fetching data and computing signals..."):
            df = _load_data(cfg.symbol, cfg.interval,
                            cfg.period, cfg.market_timezone)
            sdf = add_strategy_columns(df, cfg)

        cols = [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "vwap",
            "or_high",
            "or_low",
            "vol_spike",
            "long_signal",
            "short_signal",
        ]
        st.subheader("Latest Signals")
        st.dataframe(sdf[cols].tail(20), width='stretch')

    except Exception as exc:
        st.error(f"Signals failed: {exc}")

if run_sweep_btn:
    st.subheader("Sweep Parameters")
    c1, c2 = st.columns(2)
    opening_ranges = c1.text_input("Opening ranges", value="10,15,20")
    stop_losses = c2.text_input("Stop losses", value="0.003,0.004,0.005")
    take_profits = c1.text_input("Take profits", value="0.006,0.008,0.01")
    volume_spikes = c2.text_input("Volume spikes", value="1.1,1.2,1.4")
    top_n = st.number_input("Top rows", min_value=1,
                            max_value=100, value=10, step=1)

    if st.button("Execute Sweep", width='stretch'):
        try:
            with st.spinner("Running parameter sweep..."):
                df = _load_data(cfg.symbol, cfg.interval,
                                cfg.period, cfg.market_timezone)
                table = run_parameter_sweep(
                    df=df,
                    base_cfg=cfg,
                    opening_ranges=[int(x.strip())
                                    for x in opening_ranges.split(",") if x.strip()],
                    stop_losses=[float(x.strip())
                                 for x in stop_losses.split(",") if x.strip()],
                    take_profits=[float(x.strip())
                                  for x in take_profits.split(",") if x.strip()],
                    volume_spikes=[float(x.strip())
                                   for x in volume_spikes.split(",") if x.strip()],
                )

            if table.empty:
                st.info("No sweep results generated.")
            else:
                out_file = _save_csv(table, "web_sweep", cfg.symbol)
                st.success(f"Sweep saved to {out_file}")
                st.dataframe(table.head(int(top_n)), width='stretch')

        except Exception as exc:
            st.error(f"Sweep failed: {exc}")
