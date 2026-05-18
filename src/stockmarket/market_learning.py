"""
Market-based ML learning system: trains on market data, news sentiment, and personal trade history.
"""

from __future__ import annotations

import csv
import json
import pickle
from datetime import datetime, timedelta
from pathlib import Path
import time
from typing import Any

import numpy as np
import pandas as pd

try:
    from nsepython import nsefetch
except Exception:
    nsefetch = None

try:
    from nsepython import equity_history, quote_equity
except Exception:
    equity_history = None
    quote_equity = None

from .finnhub_client import fetch_candles, fetch_quote

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    SK_AVAILABLE = True
except Exception:
    SK_AVAILABLE = False


class MarketDataFetcher:
    """Fetch historical market data for symbols."""

    _HIST_COLS = [
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "pchange",
        "atr",
        "rsi",
        "vwap",
    ]

    @staticmethod
    def _is_nse_symbol(symbol: str) -> bool:
        return str(symbol).upper().endswith(".NS")

    @staticmethod
    def fetch_daily_ohlcv(symbol: str, days: int = 60) -> pd.DataFrame | None:
        """Fetch last N days of daily OHLCV (NSE quote API; US Finnhub)."""
        try:
            if MarketDataFetcher._is_nse_symbol(symbol):
                if nsefetch is None:
                    return None
                q = nsefetch(
                    f"https://www.nseindia.com/api/quote-equity?symbol={symbol.split('.')[0]}"
                )
                pi = q.get("priceInfo", {})
                p = float(pi.get("lastPrice") or 0)
                vwap = float(pi.get("vwap") or 0)
                hol = pi.get("intraDayHighLow", {}) or {}
                high = float(hol.get("max") or 0)
                low = float(hol.get("min") or 0)
                vol = float(pi.get("totalTradedVolume") or 0)
                pch = float(pi.get("pChange") or 0)
            else:
                sym = str(symbol).upper().strip()
                q = fetch_quote(sym)
                p = float(q.get("c") or 0.0)
                high = float(q.get("h") or p)
                low = float(q.get("l") or p)
                o = float(q.get("o") or p)
                prev_close = float(q.get("pc") or 0.0)
                vol = 0.0
                pch = (
                    ((p - prev_close) / max(prev_close, 1e-6)) * 100.0
                    if prev_close > 0
                    else 0.0
                )
                vwap = (o + high + low + p) / 4.0 if p > 0 else 0.0

            # Compute simple indicators
            atr = (high - low) if high > low else 0.0
            rsi = 50.0 + (pch * 5.0)  # Simplified RSI proxy
            rsi = max(0.0, min(100.0, rsi))

            df = pd.DataFrame(
                [{
                    "symbol": symbol,
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "close": p,
                    "high": max(p, high),
                    "low": min(p, low),
                    "volume": vol,
                    "pchange": pch,
                    "vwap": vwap,
                    "atr": atr,
                    "rsi": rsi,
                }]
            )
            return df
        except Exception:
            return None

    @staticmethod
    def compute_trend(symbol: str) -> dict[str, float]:
        """Compute trend indicators: volatility, momentum, trend direction."""
        df = MarketDataFetcher.fetch_daily_ohlcv(symbol, days=60)
        if df is None or df.empty:
            return {
                "volatility": 0.0,
                "momentum": 0.0,
                "trend": 0.0,
                "strength": 0.0,
            }

        row = df.iloc[-1].to_dict() if not df.empty else {}
        volatility = float(row.get("atr", 0.0)) / \
            max(float(row.get("close", 1.0)), 1.0) * 100.0
        momentum = float(row.get("pchange", 0.0))
        rsi = float(row.get("rsi", 50.0))
        trend = 1.0 if momentum > 0 else (-1.0 if momentum < 0 else 0.0)
        strength = min(1.0, abs(momentum) / 5.0)

        return {
            "volatility": float(volatility),
            "momentum": float(momentum),
            "trend": float(trend),
            "strength": float(strength),
            "rsi": float(rsi),
        }

    @staticmethod
    def fetch_historical_ohlcv(
        symbols: list[str],
        days: int = 60,
        cache_dir: Path | None = None,
        return_meta: bool = False,
    ) -> pd.DataFrame | tuple[pd.DataFrame, dict[str, Any]]:
        """Fetch historical OHLCV (NSE equity_history + bhavcopy; US Finnhub daily)."""

        cache_dir = cache_dir or Path("outputs") / "market_data_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        all_data = []
        meta: dict[str, Any] = {
            "cache_hits": 0,
            "network_hits": 0,
            "nse_fallback_hits": 0,
            "nse_quote_fallback_hits": 0,
            "failed_symbols": [],
            "errors": {},
            "rate_limited": False,
        }
        for sym in symbols:
            cache_file = cache_dir / f"{sym.replace('.', '_')}_history.csv"

            cached_df = MarketDataFetcher._load_cached_history(cache_file)
            if cached_df is not None and not cached_df.empty:
                all_data.append(cached_df)
                meta["cache_hits"] += 1
                continue

            fetched_df: pd.DataFrame | None = None
            fetch_error: str | None = None
            nse_error: str | None = None

            if MarketDataFetcher._is_nse_symbol(sym):
                nse_df, nse_error = MarketDataFetcher._fetch_nse_history_with_retry(
                    symbol=sym,
                    days=days,
                    max_attempts=3,
                    base_backoff_seconds=1.0,
                )
                fetched_df = nse_df
                if fetched_df is None or fetched_df.empty:
                    try:
                        from .nse_intraday import fetch_daily_equity_series_bhavcopy

                        daily = fetch_daily_equity_series_bhavcopy(sym, days + 5)
                        fetched_df = MarketDataFetcher._finalize_hist_from_ohlcv(
                            sym, daily)
                        meta["nse_fallback_hits"] += 1
                    except Exception as exc:
                        fetch_error = str(exc)
                if fetched_df is None or fetched_df.empty:
                    quote_df = MarketDataFetcher._fallback_from_nse_quote(sym)
                    if quote_df is not None and not quote_df.empty:
                        fetched_df = quote_df
                        meta["nse_quote_fallback_hits"] += 1
                    else:
                        meta["failed_symbols"].append(sym)
                        if fetch_error:
                            meta["errors"][sym] = fetch_error
                        if nse_error and sym not in meta["errors"]:
                            meta["errors"][sym] = nse_error
                        if fetch_error and (
                            "rate limit" in fetch_error.lower()
                            or "too many requests" in fetch_error.lower()
                        ):
                            meta["rate_limited"] = True
                        continue
            else:
                fetched_df, fetch_error = MarketDataFetcher._fetch_us_history_finnhub(
                    symbol=sym,
                    days=days,
                    max_attempts=4,
                    base_backoff_seconds=1.5,
                )
                if fetched_df is None or fetched_df.empty:
                    meta["failed_symbols"].append(sym)
                    if fetch_error:
                        meta["errors"][sym] = fetch_error
                        if "rate limit" in fetch_error.lower() or "too many requests" in fetch_error.lower():
                            meta["rate_limited"] = True
                    continue

            try:
                fetched_df.to_csv(cache_file, index=False)
            except Exception:
                pass

            all_data.append(fetched_df)
            meta["network_hits"] += 1

        if all_data:
            merged = pd.concat(all_data, ignore_index=True)
            if return_meta:
                return merged, meta
            return merged

        if return_meta:
            return pd.DataFrame(), meta
        return pd.DataFrame()

    @staticmethod
    def _load_cached_history(cache_file: Path) -> pd.DataFrame | None:
        if not cache_file.exists():
            return None
        try:
            cached_df = pd.read_csv(cache_file)
            if cached_df.empty:
                return None
            if "date" in cached_df.columns:
                cached_df["date"] = pd.to_datetime(
                    cached_df["date"], errors="coerce")
            for col in MarketDataFetcher._HIST_COLS:
                if col not in cached_df.columns:
                    return None
            cached_df = cached_df[MarketDataFetcher._HIST_COLS].dropna(subset=[
                                                                       "close"])
            return cached_df if not cached_df.empty else None
        except Exception:
            return None

    @staticmethod
    def _finalize_hist_from_ohlcv(symbol: str, hist: pd.DataFrame) -> pd.DataFrame:
        h = hist.copy()
        h["date"] = pd.to_datetime(h["date"], errors="coerce")
        h = h.dropna(subset=["date"])
        for c in ("open", "high", "low", "close", "volume"):
            h[c] = pd.to_numeric(h[c], errors="coerce")
        h = h.dropna(subset=["close"])
        h["symbol"] = symbol
        h["pchange"] = (
            (h["close"] - h["close"].shift(1)) / h["close"].shift(1) * 100.0
        ).fillna(0.0)
        h["atr"] = (h["high"] - h["low"]).rolling(window=5).mean().fillna(
            h["high"] - h["low"]
        )
        h["rsi"] = MarketDataFetcher._compute_rsi(h["close"], period=14)
        vw = (h["close"] * h["volume"]).rolling(window=20).sum() / h[
            "volume"
        ].rolling(window=20).sum()
        h["vwap"] = vw.fillna(h["close"])
        return h[MarketDataFetcher._HIST_COLS].dropna(subset=["close"])

    @staticmethod
    def _fetch_us_history_finnhub(
        symbol: str,
        days: int,
        max_attempts: int = 3,
        base_backoff_seconds: float = 1.0,
    ) -> tuple[pd.DataFrame | None, str | None]:
        sym = str(symbol).upper().strip()
        last_error: str | None = None
        to_u = int(time.time())
        from_u = to_u - (int(days) + 10) * 86400
        for attempt in range(max_attempts):
            try:
                raw = fetch_candles(
                    sym, resolution="D", from_unix=from_u, to_unix=to_u
                )
                if not isinstance(raw, dict) or raw.get("s") != "ok":
                    last_error = str(raw.get("s", "no data"))
                else:
                    ts = raw.get("t") or []
                    if not ts:
                        last_error = "empty Finnhub daily candles"
                    else:
                        df = pd.DataFrame(
                            {
                                "date": pd.to_datetime(
                                    pd.Series(ts, dtype="int64"), unit="s", utc=True
                                ),
                                "open": raw.get("o"),
                                "high": raw.get("h"),
                                "low": raw.get("l"),
                                "close": raw.get("c"),
                                "volume": raw.get("v"),
                            }
                        )
                        df["date"] = df["date"].dt.tz_convert(None).dt.normalize()
                        prepared = MarketDataFetcher._finalize_hist_from_ohlcv(sym, df)
                        if not prepared.empty:
                            return prepared, None
                        last_error = "prepared Finnhub history is empty"
            except Exception as exc:
                last_error = str(exc)
            if attempt < max_attempts - 1:
                time.sleep(base_backoff_seconds * (2**attempt))
        return None, last_error

    @staticmethod
    def _fetch_nse_history_with_retry(
        symbol: str,
        days: int,
        max_attempts: int = 3,
        base_backoff_seconds: float = 1.0,
    ) -> tuple[pd.DataFrame | None, str | None]:
        if equity_history is None:
            return None, "nsepython equity_history unavailable"

        ticker_symbol = symbol.split(".")[0] if "." in symbol else symbol
        start_date = (datetime.now() - timedelta(days=days + 10)
                      ).strftime("%d-%m-%Y")
        end_date = datetime.now().strftime("%d-%m-%Y")
        last_error: str | None = None

        for attempt in range(max_attempts):
            try:
                hist = equity_history(
                    ticker_symbol, "EQ", start_date, end_date)
                if hist is None or len(hist) == 0:
                    last_error = "NSE history empty"
                else:
                    prepared = MarketDataFetcher._prepare_nse_history_df(
                        symbol, hist)
                    if prepared is not None and not prepared.empty:
                        return prepared, None
                    last_error = "NSE history parse failed"
            except Exception as exc:
                last_error = str(exc)

            if attempt < max_attempts - 1:
                time.sleep(base_backoff_seconds * (2 ** attempt))

        return None, last_error

    @staticmethod
    def _prepare_nse_history_df(symbol: str, hist: pd.DataFrame) -> pd.DataFrame | None:
        try:
            df = hist.copy()
            colmap = {
                "CH_TIMESTAMP": "date",
                "CH_OPENING_PRICE": "open",
                "CH_TRADE_HIGH_PRICE": "high",
                "CH_TRADE_LOW_PRICE": "low",
                "CH_CLOSING_PRICE": "close",
                "CH_TOT_TRADED_QTY": "volume",
            }
            missing = [c for c in colmap if c not in df.columns]
            if missing:
                return None
            df = df.rename(columns=colmap)
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna(subset=["date", "close"]).sort_values("date")
            if df.empty:
                return None
            df["symbol"] = symbol
            df["pchange"] = (
                (df["close"] - df["close"].shift(1)) /
                df["close"].shift(1) * 100.0
            ).fillna(0.0)
            df["atr"] = (df["high"] - df["low"]
                         ).rolling(window=5).mean().fillna(df["high"] - df["low"])
            df["rsi"] = MarketDataFetcher._compute_rsi(df["close"], period=14)
            df["vwap"] = (
                (df["close"] * df["volume"]).rolling(window=20).sum()
                / df["volume"].rolling(window=20).sum()
            ).fillna(df["close"])
            return df[MarketDataFetcher._HIST_COLS]
        except Exception:
            return None

    @staticmethod
    def _fallback_from_nse_quote(symbol: str) -> pd.DataFrame | None:
        if quote_equity is None:
            return None
        try:
            ticker_symbol = symbol.split(".")[0] if "." in symbol else symbol
            q = quote_equity(ticker_symbol)
            pi = (q or {}).get("priceInfo", {})
            p = float(pi.get("lastPrice") or 0.0)
            high = float(((pi.get("intraDayHighLow") or {}).get("max")) or p)
            low = float(((pi.get("intraDayHighLow") or {}).get("min")) or p)
            vol = float(pi.get("totalTradedVolume") or 0.0)
            pch = float(pi.get("pChange") or 0.0)
            if p <= 0:
                return None

            # Build a tiny synthetic series to bootstrap feature extraction when all APIs are blocked.
            rows = []
            for i in range(6, 0, -1):
                dt = datetime.now() - timedelta(days=i)
                drift = (pch / 100.0) * (1.0 - i / 10.0)
                close_i = max(0.01, p * (1.0 - drift))
                rows.append(
                    {
                        "symbol": symbol,
                        "date": dt,
                        "open": close_i,
                        "high": max(close_i, high),
                        "low": min(close_i, low),
                        "close": close_i,
                        "volume": max(vol, 1.0),
                    }
                )
            df = pd.DataFrame(rows)
            df["pchange"] = ((df["close"] - df["close"].shift(1)) /
                             df["close"].shift(1) * 100.0).fillna(0.0)
            df["atr"] = (df["high"] - df["low"]
                         ).rolling(window=3).mean().fillna(df["high"] - df["low"])
            df["rsi"] = MarketDataFetcher._compute_rsi(df["close"], period=6)
            df["vwap"] = df["close"]
            return df[MarketDataFetcher._HIST_COLS]
        except Exception:
            return None

    @staticmethod
    def _compute_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
        """Compute Relative Strength Index."""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss.replace(0, 1)
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50.0)


class PersonalTradeAnalyzer:
    """Analyze personal trade history to extract per-symbol performance."""

    @staticmethod
    def analyze_trades(state_file: Path) -> dict[str, dict[str, Any]]:
        """Extract symbol-level stats from trade log."""
        if not state_file.exists():
            return {}

        try:
            state = json.loads(state_file.read_text())
        except Exception:
            return {}

        log = state.get("log", [])
        symbol_stats: dict[str, dict[str, Any]] = {}

        for entry in log:
            side = str(entry.get("side", "")).upper()
            if side not in {"BUY", "SHORT"}:
                continue

            sym = str(entry.get("symbol", ""))
            if not sym:
                continue

            if sym not in symbol_stats:
                symbol_stats[sym] = {
                    "total_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "total_pnl": 0.0,
                    "avg_win": 0.0,
                    "avg_loss": 0.0,
                    "win_rate": 0.0,
                    "sides_used": set(),
                }

            symbol_stats[sym]["total_trades"] += 1
            symbol_stats[sym]["sides_used"].add(side)

            pnl = float(entry.get("realized_delta", 0.0) or 0.0)
            symbol_stats[sym]["total_pnl"] += pnl

            if pnl > 0:
                symbol_stats[sym]["wins"] += 1
                if symbol_stats[sym]["wins"] > 0:
                    symbol_stats[sym]["avg_win"] = (
                        symbol_stats[sym]["total_pnl"] /
                        symbol_stats[sym]["wins"]
                    )
            else:
                symbol_stats[sym]["losses"] += 1
                if symbol_stats[sym]["losses"] > 0:
                    symbol_stats[sym]["avg_loss"] = (
                        symbol_stats[sym]["total_pnl"] /
                        symbol_stats[sym]["losses"]
                    )

            total = symbol_stats[sym]["wins"] + symbol_stats[sym]["losses"]
            if total > 0:
                symbol_stats[sym]["win_rate"] = (
                    symbol_stats[sym]["wins"] / total
                )

        return symbol_stats


class FeatureEngineer:
    """Engineer features combining market data, sentiment, and personal history."""

    @staticmethod
    def create_feature_vector(
        symbol: str,
        state_file: Path,
        market_data: dict[str, Any] | None = None,
        sentiment: float = 0.0,
    ) -> dict[str, float]:
        """Create feature vector for a symbol."""
        features = {
            "symbol": symbol,
            "volatility": 0.0,
            "momentum": 0.0,
            "trend": 0.0,
            "strength": 0.0,
            "rsi": 50.0,
            "sentiment": 0.0,
            "win_rate": 0.0,
            "total_trades": 0.0,
            "total_pnl": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
        }

        # Market data
        if market_data:
            features.update(market_data)

        # Sentiment
        features["sentiment"] = float(sentiment)

        # Personal trade history
        trade_stats = PersonalTradeAnalyzer.analyze_trades(
            state_file).get(symbol, {})
        if trade_stats:
            features["win_rate"] = float(trade_stats.get("win_rate", 0.0))
            features["total_trades"] = float(
                trade_stats.get("total_trades", 0.0))
            features["total_pnl"] = float(trade_stats.get("total_pnl", 0.0))
            features["avg_win"] = float(trade_stats.get("avg_win", 0.0))
            features["avg_loss"] = float(trade_stats.get("avg_loss", 0.0))

        return features


class MarketLearningModel:
    """Ensemble ML model for symbol quality scoring."""

    def __init__(self, model_path: Path | None = None):
        self.model_path = model_path or Path(
            "outputs") / "market_learning_model.pkl"
        self.lr_model = None
        self.rf_model = None
        self.scaler = None
        self.feature_names = [
            "volatility",
            "momentum",
            "trend",
            "strength",
            "rsi",
            "sentiment",
            "win_rate",
            "total_trades",
            "total_pnl",
            "avg_win",
            "avg_loss",
        ]
        if self.model_path.exists():
            self.load()

    def train(
        self,
        symbol_data: list[dict[str, float]],
        labels: list[int],
    ) -> dict[str, Any]:
        """Train model on symbol features and quality labels (1=good, 0=bad)."""
        if not SK_AVAILABLE or len(symbol_data) < 5:
            return {"status": "skipped", "reason": "insufficient data or sklearn unavailable"}
        if len(set(int(x) for x in labels)) < 2:
            return {"status": "skipped", "reason": "insufficient label diversity (single class)"}

        try:
            X = np.array(
                [[d.get(f, 0.0) for f in self.feature_names]
                 for d in symbol_data]
            )
            y = np.array(labels)

            self.scaler = StandardScaler()
            X_scaled = self.scaler.fit_transform(X)

            self.lr_model = LogisticRegression(max_iter=1000, random_state=42)
            self.lr_model.fit(X_scaled, y)

            self.rf_model = RandomForestClassifier(
                n_estimators=50, max_depth=5, random_state=42
            )
            self.rf_model.fit(X_scaled, y)

            self.save()

            lr_score = float(self.lr_model.score(X_scaled, y))
            rf_score = float(self.rf_model.score(X_scaled, y))

            return {
                "status": "trained",
                "lr_accuracy": lr_score,
                "rf_accuracy": rf_score,
                "samples": len(symbol_data),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def predict_probability(self, feature_vector: dict[str, float]) -> float:
        """Predict quality probability for a symbol."""
        if self.lr_model is None or self.scaler is None or not SK_AVAILABLE:
            return 0.5

        try:
            X = np.array([[feature_vector.get(f, 0.0)
                         for f in self.feature_names]])
            X_scaled = self.scaler.transform(X)

            lr_prob = float(self.lr_model.predict_proba(X_scaled)[0, 1])
            rf_prob = float(self.rf_model.predict_proba(X_scaled)[0, 1])

            return (lr_prob + rf_prob) / 2.0
        except Exception:
            return 0.5

    def save(self) -> None:
        """Persist model to disk."""
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.model_path, "wb") as f:
                pickle.dump(
                    {
                        "lr_model": self.lr_model,
                        "rf_model": self.rf_model,
                        "scaler": self.scaler,
                        "feature_names": self.feature_names,
                    },
                    f,
                )
        except Exception:
            pass

    def load(self) -> None:
        """Load model from disk."""
        if not self.model_path.exists():
            return
        try:
            with open(self.model_path, "rb") as f:
                data = pickle.load(f)
                self.lr_model = data.get("lr_model")
                self.rf_model = data.get("rf_model")
                self.scaler = data.get("scaler")
                self.feature_names = data.get(
                    "feature_names", self.feature_names)
        except Exception:
            pass


def resolve_learning_model_path(state_file: Path) -> Path:
    """Map each trade state file to its own persisted ML model file."""
    stem = state_file.stem
    if stem == "simple_paper_state":
        model_name = "market_learning_model.pkl"
    elif stem.startswith("simple_paper_state"):
        suffix = stem[len("simple_paper_state"):]
        model_name = f"market_learning_model{suffix}.pkl"
    else:
        model_name = f"{stem}_market_learning_model.pkl"
    return state_file.with_name(model_name)


def train_market_learning_model(
    state_file: Path,
    watchlist: list[str],
    use_historical_data: bool = True,
    historical_days: int = 60,
) -> dict[str, Any]:
    """
    Main orchestration: build training dataset from market + personal history,
    then train ensemble model.

    If use_historical_data=True, fetches 60+ days of historical OHLCV from Finnhub (US)
    or NSE (India: equity_history with bhavcopy fallback).
    and bootstraps the model with price-action patterns before using trade history.
    """
    if not SK_AVAILABLE:
        return {"status": "skipped", "reason": "scikit-learn not installed"}

    symbol_data = []
    labels = []
    training_samples = 0
    historical_symbols_covered = 0
    forward_return_threshold_pct = 0.35
    label_rebalanced = False
    hist_meta: dict[str, Any] = {
        "cache_hits": 0,
        "network_hits": 0,
        "nse_fallback_hits": 0,
        "nse_quote_fallback_hits": 0,
        "failed_symbols": [],
        "errors": {},
        "rate_limited": False,
    }

    trade_stats = PersonalTradeAnalyzer.analyze_trades(state_file)

    # Phase 1: Train on historical OHLCV (if available)
    if use_historical_data:
        try:
            hist_df, hist_meta = MarketDataFetcher.fetch_historical_ohlcv(
                watchlist,
                days=historical_days,
                cache_dir=Path("outputs") / "market_data_cache",
                return_meta=True,
            )
            if not hist_df.empty:
                hist_df = hist_df.copy()
                hist_df["date"] = pd.to_datetime(
                    hist_df["date"], errors="coerce")
                hist_df = hist_df.dropna(
                    subset=["date", "close"]).sort_values(["symbol", "date"])
                for sym in watchlist:
                    sym_hist = hist_df[hist_df["symbol"] ==
                                       sym].copy().reset_index(drop=True)
                    if len(sym_hist) < 6:
                        continue

                    historical_symbols_covered += 1
                    close = pd.to_numeric(sym_hist["close"], errors="coerce")
                    next_return_pct = (
                        (close.shift(-1) - close) / close * 100.0)
                    sym_trade = trade_stats.get(sym, {})
                    base_win_rate = float(sym_trade.get(
                        "win_rate", 0.5)) if sym_trade else 0.5
                    base_total_trades = float(sym_trade.get(
                        "total_trades", 0.0)) if sym_trade else 0.0
                    base_total_pnl = float(sym_trade.get(
                        "total_pnl", 0.0)) if sym_trade else 0.0
                    base_avg_win = float(sym_trade.get(
                        "avg_win", 0.0)) if sym_trade else 0.0
                    base_avg_loss = float(sym_trade.get(
                        "avg_loss", 0.0)) if sym_trade else 0.0

                    for i in range(0, len(sym_hist) - 1):
                        row = sym_hist.iloc[i]
                        price = float(row.get("close", 0.0) or 0.0)
                        if price <= 0:
                            continue

                        momentum = float(row.get("pchange", 0.0) or 0.0)
                        atr = float(row.get("atr", 0.0) or 0.0)
                        rsi = float(row.get("rsi", 50.0) or 50.0)
                        volatility = (atr / max(price, 1e-6)) * 100.0
                        trend_score = 1.0 if momentum > 0 else (
                            -1.0 if momentum < 0 else 0.0)

                        features = {
                            "volatility": float(volatility),
                            "momentum": float(momentum),
                            "trend": float(trend_score),
                            "strength": float(min(1.0, abs(momentum) / 5.0)),
                            "rsi": float(rsi),
                            "sentiment": 0.0,
                            "win_rate": float(base_win_rate),
                            "total_trades": float(base_total_trades),
                            "total_pnl": float(base_total_pnl),
                            "avg_win": float(base_avg_win),
                            "avg_loss": float(base_avg_loss),
                        }

                        fwd = float(next_return_pct.iloc[i]) if pd.notna(
                            next_return_pct.iloc[i]) else 0.0
                        label = 1 if fwd >= float(
                            forward_return_threshold_pct) else 0
                        symbol_data.append(features)
                        labels.append(label)
                        training_samples += 1
        except Exception as e:
            return {"status": "error", "reason": f"Historical fetch failed: {e}"}

    # Phase 2: Overlay with personal trade history to refine labels
    trade_refined_samples = 0
    for sym in watchlist:
        stats = trade_stats.get(sym, {})
        if stats.get("total_trades", 0) > 0:
            market_trend = MarketDataFetcher.compute_trend(sym)
            features = FeatureEngineer.create_feature_vector(
                sym, state_file, market_data=market_trend, sentiment=0.0
            )
            win_rate = float(stats.get("win_rate", 0.0))
            label = 1 if win_rate >= 0.55 else 0

            symbol_data.append(features)
            labels.append(label)
            trade_refined_samples += 1

    # If labels collapse into one class (common during API fallback phases),
    # rebalance using cross-sectional momentum ranking to keep training usable.
    if len(symbol_data) >= 10 and len(set(int(x) for x in labels)) < 2:
        ranked_idx = sorted(
            range(len(symbol_data)),
            key=lambda i: float(symbol_data[i].get("momentum", 0.0) or 0.0),
        )
        split = max(1, len(ranked_idx) // 2)
        rebalanced = [0] * len(ranked_idx)
        for idx in ranked_idx[split:]:
            rebalanced[idx] = 1
        labels = rebalanced
        label_rebalanced = True

    if len(symbol_data) < 5:
        short_result: dict[str, Any] = {
            "status": "insufficient_data",
            "reason": "Need at least 5 training samples",
            "collected": len(symbol_data),
        }
        short_result["historical_data_samples"] = training_samples
        short_result["historical_symbols_covered"] = historical_symbols_covered
        short_result["personal_trade_samples"] = trade_refined_samples
        short_result["total_training_samples"] = len(symbol_data)
        short_result["historical_cache_hits"] = int(
            hist_meta.get("cache_hits", 0))
        short_result["historical_network_hits"] = int(
            hist_meta.get("network_hits", 0))
        short_result["nse_fallback_hits"] = int(
            hist_meta.get("nse_fallback_hits", 0))
        short_result["nse_quote_fallback_hits"] = int(
            hist_meta.get("nse_quote_fallback_hits", 0))
        short_result["historical_failed_symbols"] = list(
            hist_meta.get("failed_symbols", []))
        short_result["label_rebalanced"] = bool(label_rebalanced)
        if hist_meta.get("rate_limited"):
            short_result["historical_warning"] = "Data provider rate limit detected; using cache where available."
        return short_result

    model = MarketLearningModel(resolve_learning_model_path(state_file))
    result = model.train(symbol_data, labels)
    result["symbols_trained"] = len(watchlist)
    result["total_training_samples"] = len(symbol_data)
    result["historical_data_samples"] = training_samples
    result["historical_symbols_covered"] = historical_symbols_covered
    result["personal_trade_samples"] = trade_refined_samples
    result["historical_cache_hits"] = int(hist_meta.get(
        "cache_hits", 0)) if isinstance(hist_meta, dict) else 0
    result["historical_network_hits"] = int(hist_meta.get(
        "network_hits", 0)) if isinstance(hist_meta, dict) else 0
    result["nse_fallback_hits"] = int(hist_meta.get(
        "nse_fallback_hits", 0)) if isinstance(hist_meta, dict) else 0
    result["nse_quote_fallback_hits"] = int(hist_meta.get(
        "nse_quote_fallback_hits", 0)) if isinstance(hist_meta, dict) else 0
    if isinstance(hist_meta, dict):
        result["historical_failed_symbols"] = list(
            hist_meta.get("failed_symbols", []))
        if hist_meta.get("rate_limited"):
            result["historical_warning"] = "Data provider rate limit detected; using cache where available."
    result["label_rebalanced"] = bool(label_rebalanced)
    result["training_note"] = (
        f"Rolling bootstrap: {training_samples} historical rows across {historical_symbols_covered} symbols; "
        f"refined by {trade_refined_samples} personal trade samples"
    )

    return result


def get_symbol_quality_score(
    symbol: str,
    state_file: Path,
) -> float:
    """Fetch or compute ML-based quality score for a symbol (0-1)."""
    market_trend = MarketDataFetcher.compute_trend(symbol)
    features = FeatureEngineer.create_feature_vector(
        symbol, state_file, market_data=market_trend, sentiment=0.0
    )

    model = MarketLearningModel(resolve_learning_model_path(state_file))
    prob = model.predict_probability(features)

    return min(1.0, max(0.0, prob))
