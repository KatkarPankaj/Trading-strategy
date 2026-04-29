"""
Market-based ML learning system: trains on market data, news sentiment, and personal trade history.
"""

from __future__ import annotations

import csv
import json
import pickle
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from nsepython import nsefetch
except Exception:
    nsefetch = None

try:
    import yfinance as yf
    YF_AVAILABLE = True
except Exception:
    YF_AVAILABLE = False

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    SK_AVAILABLE = True
except Exception:
    SK_AVAILABLE = False


class MarketDataFetcher:
    """Fetch historical market data for symbols."""

    @staticmethod
    def fetch_daily_ohlcv(symbol: str, days: int = 60) -> pd.DataFrame | None:
        """Fetch last N days of daily OHLCV. Simulated for now."""
        # In production, use yfinance or NSE API; for now, return mock data
        # Real implementation would call actual market data API
        try:
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
    def fetch_historical_ohlcv(symbols: list[str], days: int = 60, cache_dir: Path | None = None) -> pd.DataFrame:
        """Fetch historical OHLCV data from yfinance (NSE stocks)."""
        if not YF_AVAILABLE:
            return pd.DataFrame()

        cache_dir = cache_dir or Path("outputs") / "market_data_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        all_data = []
        for sym in symbols:
            cache_file = cache_dir / f"{sym.replace('.', '_')}_history.csv"

            # Try to load from cache first
            if cache_file.exists():
                try:
                    cached_df = pd.read_csv(cache_file)
                    cached_df["date"] = pd.to_datetime(cached_df["date"])
                    all_data.append(cached_df)
                    continue
                except Exception:
                    pass

            # Fetch from yfinance
            try:
                ticker_symbol = sym.split(".")[0] if "." in sym else sym
                ticker = yf.Ticker(f"{ticker_symbol}.NS")
                hist = ticker.history(period=f"{days}d")

                if hist.empty:
                    continue

                hist = hist.reset_index()
                hist.columns = ["date", "open", "high", "low",
                                "close", "volume", "dividends", "stock_splits"]
                hist["symbol"] = sym
                hist["pchange"] = ((hist["close"] - hist["close"].shift(1)) /
                                   hist["close"].shift(1) * 100.0).fillna(0.0)
                hist["atr"] = (hist["high"] - hist["low"]).rolling(
                    window=5).mean().fillna(hist["high"] - hist["low"])
                hist["rsi"] = MarketDataFetcher._compute_rsi(
                    hist["close"], period=14)
                hist["vwap"] = (hist["close"] * hist["volume"]).rolling(
                    window=20).sum() / hist["volume"].rolling(window=20).sum()
                hist["vwap"] = hist["vwap"].fillna(hist["close"])

                # Keep only relevant columns
                hist = hist[["symbol", "date", "open", "high", "low",
                             "close", "volume", "pchange", "atr", "rsi", "vwap"]]

                # Cache the result
                try:
                    hist.to_csv(cache_file, index=False)
                except Exception:
                    pass

                all_data.append(hist)
            except Exception:
                pass

        if all_data:
            return pd.concat(all_data, ignore_index=True)
        return pd.DataFrame()

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


def train_market_learning_model(
    state_file: Path,
    watchlist: list[str],
    use_historical_data: bool = True,
    historical_days: int = 60,
) -> dict[str, Any]:
    """
    Main orchestration: build training dataset from market + personal history,
    then train ensemble model.

    If use_historical_data=True, fetches 60+ days of historical OHLCV from yfinance
    and bootstraps the model with price-action patterns before using trade history.
    """
    if not SK_AVAILABLE:
        return {"status": "skipped", "reason": "scikit-learn not installed"}

    symbol_data = []
    labels = []
    training_samples = 0

    # Phase 1: Train on historical OHLCV (if available)
    if use_historical_data and YF_AVAILABLE:
        try:
            hist_df = MarketDataFetcher.fetch_historical_ohlcv(
                watchlist, days=historical_days, cache_dir=Path(
                    "outputs") / "market_data_cache"
            )
            if not hist_df.empty:
                for sym in watchlist:
                    sym_hist = hist_df[hist_df["symbol"] == sym]
                    if len(sym_hist) >= 5:
                        # Use recent price action as proxy for quality
                        recent_avg_pchange = float(
                            sym_hist["pchange"].tail(10).mean())
                        volatility = float(
                            sym_hist["atr"].mean() / sym_hist["close"].mean() * 100.0)
                        momentum = float(sym_hist["pchange"].iloc[-1])
                        rsi = float(sym_hist["rsi"].iloc[-1])
                        trend_score = 1.0 if momentum > 0 else 0.0

                        features = {
                            "volatility": volatility,
                            "momentum": momentum,
                            "trend": trend_score,
                            "strength": min(1.0, abs(momentum) / 5.0),
                            "rsi": rsi,
                            "sentiment": 0.0,
                            "win_rate": 0.5,  # Neutral for historical data
                            "total_trades": 0.0,
                            "total_pnl": 0.0,
                            "avg_win": 0.0,
                            "avg_loss": 0.0,
                        }

                        symbol_data.append(features)
                        # Label: positive if average uptrend + decent RSI, else negative
                        label = 1 if recent_avg_pchange > 0.5 and 40 < rsi < 70 else 0
                        labels.append(label)
                        training_samples += 1
        except Exception as e:
            return {"status": "error", "reason": f"Historical fetch failed: {e}"}

    # Phase 2: Overlay with personal trade history to refine labels
    trade_stats = PersonalTradeAnalyzer.analyze_trades(state_file)
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

    if len(symbol_data) < 5:
        return {
            "status": "insufficient_data",
            "reason": "Need at least 5 training samples",
            "collected": len(symbol_data),
        }

    model = MarketLearningModel()
    result = model.train(symbol_data, labels)
    result["symbols_trained"] = len(watchlist)
    result["total_training_samples"] = len(symbol_data)
    result["historical_data_samples"] = training_samples
    result["personal_trade_samples"] = trade_refined_samples
    result["training_note"] = f"Bootstrap: {training_samples} historical samples, refined by {trade_refined_samples} personal trades"

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

    model = MarketLearningModel()
    prob = model.predict_probability(features)

    return min(1.0, max(0.0, prob))
