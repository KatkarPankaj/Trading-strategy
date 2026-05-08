"""Reusable Streamlit UI components."""

import streamlit as st
import pandas as pd
from typing import Dict, List, Optional

from ..utils.logger import AppLogger


def render_market_selector(default: str = "NSE") -> str:
    """Render market selector widget.
    
    Args:
        default: Default market selection
        
    Returns:
        Selected market
    """
    return st.selectbox(
        "📈 Select Market",
        options=["NSE", "US"],
        index=0 if default == "NSE" else 1,
        key="selected_market"
    )


def render_sidebar_config(trading_config: Dict) -> Dict:
    """Render sidebar configuration inputs.
    
    Args:
        trading_config: Current trading configuration
        
    Returns:
        Updated configuration dictionary
    """
    st.sidebar.header("⚙️ Trading Configuration")
    
    config = {}
    
    # Capital settings
    config['starting_capital'] = st.sidebar.number_input(
        "💰 Starting Capital",
        value=trading_config.get('starting_capital', 200000.0),
        min_value=10000.0,
        key="starting_capital"
    )
    
    config['max_trades_per_day'] = st.sidebar.number_input(
        "📊 Max Trades/Day",
        value=trading_config.get('max_trades_per_day', 1),
        min_value=1,
        key="max_trades_per_day"
    )
    
    config['risk_per_trade_pct'] = st.sidebar.slider(
        "⚠️ Risk per Trade (%)",
        min_value=0.01,
        max_value=5.0,
        value=trading_config.get('risk_per_trade_pct', 0.5),
        step=0.01,
        key="risk_per_trade_pct"
    )
    
    # Stop loss and take profit
    col1, col2 = st.sidebar.columns(2)
    with col1:
        config['stop_loss_pct'] = st.number_input(
            "🛑 Stop Loss (%)",
            value=trading_config.get('stop_loss_pct', 0.4),
            min_value=0.01,
            key="stop_loss_pct"
        )
    
    with col2:
        config['take_profit_pct'] = st.number_input(
            "🎯 Take Profit (%)",
            value=trading_config.get('take_profit_pct', 0.8),
            min_value=0.01,
            key="take_profit_pct"
        )
    
    config['allow_short'] = st.sidebar.checkbox(
        "📉 Allow Short Selling",
        value=trading_config.get('allow_short', False),
        key="allow_short"
    )
    
    return config


def render_portfolio_summary(summary: Dict) -> None:
    """Render portfolio summary metrics.
    
    Args:
        summary: Portfolio summary dictionary
    """
    st.header("📊 Portfolio Summary")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric(
            "Starting Capital",
            f"₹{summary.get('starting_capital', 0):,.2f}"
        )
    
    with col2:
        st.metric(
            "Current Value",
            f"₹{summary.get('total_value', 0):,.2f}",
            delta=f"{summary.get('total_pnl_pct', 0):.2f}%"
        )
    
    with col3:
        pnl = summary.get('total_pnl', 0)
        pnl_color = "green" if pnl >= 0 else "red"
        st.metric(
            "Total P&L",
            f"₹{pnl:,.2f}",
            delta_color="normal" if pnl >= 0 else "inverse"
        )
    
    with col4:
        st.metric(
            "Positions",
            f"{summary.get('open_positions', 0)} Open",
            f"{summary.get('closed_trades', 0)} Closed"
        )
    
    st.divider()


def render_positions_table(positions_df: pd.DataFrame) -> None:
    """Render current positions table.
    
    Args:
        positions_df: DataFrame with position data
    """
    if positions_df.empty:
        st.info("📭 No open positions")
        return
    
    st.subheader("📍 Open Positions")
    
    # Format display
    display_df = positions_df.copy()
    if 'Avg Price' in display_df.columns:
        display_df['Avg Price'] = display_df['Avg Price'].apply(lambda x: f"₹{x:.2f}")
    if 'Current Price' in display_df.columns:
        display_df['Current Price'] = display_df['Current Price'].apply(lambda x: f"₹{x:.2f}")
    if 'Value' in display_df.columns:
        display_df['Value'] = display_df['Value'].apply(lambda x: f"₹{x:,.2f}")
    if 'PnL' in display_df.columns:
        display_df['PnL'] = display_df['PnL'].apply(lambda x: f"₹{x:,.2f}")
    if 'PnL %' in display_df.columns:
        display_df['PnL %'] = display_df['PnL %'].apply(lambda x: f"{x:+.2f}%")
    
    st.dataframe(display_df, width='stretch')


def render_trades_table(trades: List[Dict]) -> None:
    """Render recent trades table.
    
    Args:
        trades: List of trade dictionaries
    """
    if not trades:
        st.info("📭 No trades yet")
        return
    
    st.subheader("📈 Recent Trades")
    
    # Convert to DataFrame
    trades_df = pd.DataFrame(trades)
    
    # Select display columns
    display_cols = ['symbol', 'trade_type', 'qty', 'entry_price', 'exit_price', 'pnl', 'status']
    display_df = trades_df[[col for col in display_cols if col in trades_df.columns]]
    
    st.dataframe(display_df, width='stretch')


def render_app_logs(logs: List[str], title: str = "📋 Application Logs") -> None:
    """Render application logs in expandable section.
    
    Args:
        logs: List of formatted log strings
        title: Section title
    """
    if not logs:
        return
    
    with st.expander(title, expanded=False):
        # Display logs in reverse order (newest first)
        log_text = "\n".join(reversed(logs[-50:]))
        st.markdown(log_text)


def render_watchlist_selector(watchlist: List[str], default_selected: List[str] = None) -> List[str]:
    """Render watchlist selector widget.
    
    Args:
        watchlist: Available symbols
        default_selected: Default selected symbols
        
    Returns:
        Selected symbols
    """
    if default_selected is None:
        default_selected = watchlist[:5]
    
    st.subheader("📋 Select Watchlist Symbols")
    
    selected = st.multiselect(
        "Symbols to monitor",
        options=watchlist,
        default=default_selected,
        key="watchlist_select"
    )
    
    return selected


def render_price_chart(prices: Dict[str, float], symbols: List[str]) -> None:
    """Render price chart for symbols.
    
    Args:
        prices: Dictionary of symbol:price pairs
        symbols: Symbols to display
    """
    if not symbols or not prices:
        st.info("No price data available")
        return
    
    st.subheader("📊 Current Prices")
    
    # Create DataFrame for display
    data = {
        'Symbol': symbols,
        'Price': [prices.get(symbol, 0.0) for symbol in symbols]
    }
    prices_df = pd.DataFrame(data)
    
    st.dataframe(prices_df, width='stretch')


def render_error_message(message: str) -> None:
    """Render error message.
    
    Args:
        message: Error message text
    """
    st.error(f"❌ {message}")


def render_success_message(message: str) -> None:
    """Render success message.
    
    Args:
        message: Success message text
    """
    st.success(f"✅ {message}")


def render_warning_message(message: str) -> None:
    """Render warning message.
    
    Args:
        message: Warning message text
    """
    st.warning(f"⚠️ {message}")


def render_info_message(message: str) -> None:
    """Render info message.
    
    Args:
        message: Info message text
    """
    st.info(f"ℹ️ {message}")
