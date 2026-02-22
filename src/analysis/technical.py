"""
Technical analysis module for computing indicators on price data.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

logger = get_logger("analysis.technical")


class TechnicalAnalyzer:
    """Computes technical indicators from OHLCV candle data."""

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.rsi_period = cfg.get("rsi_period", 14)
        self.rsi_overbought = cfg.get("rsi_overbought", 70)
        self.rsi_oversold = cfg.get("rsi_oversold", 30)
        self.macd_fast = cfg.get("macd_fast", 12)
        self.macd_slow = cfg.get("macd_slow", 26)
        self.macd_signal = cfg.get("macd_signal", 9)
        self.bb_period = cfg.get("bb_period", 20)
        self.bb_std = cfg.get("bb_std", 2)
        self.ema_periods = cfg.get("ema_periods", [9, 21, 50, 200])

    def candles_to_dataframe(self, candles: list[dict]) -> pd.DataFrame:
        """Convert Coinbase candle data to pandas DataFrame."""
        if not candles:
            return pd.DataFrame()

        df = pd.DataFrame(candles)

        # Coinbase returns: start, low, high, open, close, volume
        numeric_cols = ["open", "high", "low", "close", "volume"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if "start" in df.columns:
            df["timestamp"] = pd.to_numeric(df["start"], errors="coerce")
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
            df = df.sort_values("timestamp").reset_index(drop=True)

        return df

    def compute_rsi(self, df: pd.DataFrame, period: Optional[int] = None) -> pd.Series:
        """Compute Relative Strength Index."""
        period = period or self.rsi_period
        close = df["close"]
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)

        avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def compute_macd(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Compute MACD, Signal line, and Histogram."""
        close = df["close"]
        ema_fast = close.ewm(span=self.macd_fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.macd_slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        signal = macd.ewm(span=self.macd_signal, adjust=False).mean()
        histogram = macd - signal
        return macd, signal, histogram

    def compute_bollinger_bands(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Compute Bollinger Bands (upper, middle, lower)."""
        close = df["close"]
        middle = close.rolling(window=self.bb_period).mean()
        std = close.rolling(window=self.bb_period).std()
        upper = middle + (std * self.bb_std)
        lower = middle - (std * self.bb_std)
        return upper, middle, lower

    def compute_ema(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Compute Exponential Moving Average."""
        return df["close"].ewm(span=period, adjust=False).mean()

    def compute_sma(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Compute Simple Moving Average."""
        return df["close"].rolling(window=period).mean()

    def compute_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Compute Average True Range."""
        high = df["high"]
        low = df["low"]
        close = df["close"]

        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()
        return atr

    def compute_adx(self, df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
        """
        Compute Average Directional Index (ADX) with +DI and -DI.

        ADX > 25 → trending market (trend-following strategies)
        ADX < 20 → ranging market (mean-reversion strategies)
        """
        high = df["high"]
        low = df["low"]
        close = df["close"]

        # Directional Movement
        plus_dm = high.diff()
        minus_dm = -low.diff()

        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

        # True Range
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # Smoothed averages
        atr = tr.ewm(alpha=1 / period, min_periods=period).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr)
        minus_di = 100 * (minus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr)

        # ADX = smoothed average of DX
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.ewm(alpha=1 / period, min_periods=period).mean()

        return adx, plus_di, minus_di

    def compute_stochastic_rsi(
        self, df: pd.DataFrame, rsi_period: int = 14, stoch_period: int = 14, k_smooth: int = 3, d_smooth: int = 3
    ) -> tuple[pd.Series, pd.Series]:
        """
        Compute Stochastic RSI (%K and %D lines).

        Better at detecting overbought/oversold in ranging markets vs plain RSI.
        %K < 20 → oversold, %K > 80 → overbought
        """
        rsi = self.compute_rsi(df, rsi_period)

        # Stochastic of RSI
        rsi_low = rsi.rolling(window=stoch_period).min()
        rsi_high = rsi.rolling(window=stoch_period).max()

        stoch_rsi = ((rsi - rsi_low) / (rsi_high - rsi_low).replace(0, np.nan)) * 100
        k_line = stoch_rsi.rolling(window=k_smooth).mean()
        d_line = k_line.rolling(window=d_smooth).mean()

        return k_line, d_line

    def compute_obv(self, df: pd.DataFrame) -> pd.Series:
        """
        Compute On-Balance Volume (OBV).

        Measures cumulative buying/selling pressure via volume flow.
        Divergence between OBV and price signals potential reversals.
        """
        close = df["close"]
        volume = df["volume"]

        direction = np.sign(close.diff())
        direction.iloc[0] = 0
        obv = (direction * volume).cumsum()
        return obv

    def compute_vwap(self, df: pd.DataFrame) -> pd.Series:
        """
        Compute Volume-Weighted Average Price (VWAP).

        Institutional fair-value reference for intraday trading.
        Price > VWAP → bullish bias, Price < VWAP → bearish bias
        """
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        cumulative_tp_vol = (typical_price * df["volume"]).cumsum()
        cumulative_vol = df["volume"].cumsum().replace(0, np.nan)
        vwap = cumulative_tp_vol / cumulative_vol
        return vwap

    def compute_volume_sma(self, df: pd.DataFrame, period: int = 20) -> pd.Series:
        """Compute volume Simple Moving Average."""
        return df["volume"].rolling(window=period).mean()

    def compute_support_resistance(
        self, df: pd.DataFrame, window: int = 20
    ) -> tuple[float, float]:
        """Estimate support and resistance levels."""
        if len(df) < window:
            return df["low"].min(), df["high"].max()

        recent = df.tail(window)
        support = recent["low"].min()
        resistance = recent["high"].max()
        return support, resistance

    def analyze(self, candles: list[dict]) -> dict:
        """
        Perform full technical analysis on candle data.
        Returns a dictionary of all computed indicators and signals.
        """
        df = self.candles_to_dataframe(candles)

        if df.empty or len(df) < 30:
            logger.warning("Not enough candle data for analysis")
            return {"error": "Insufficient data", "candle_count": len(df)}

        current_price = df["close"].iloc[-1]
        prev_price = df["close"].iloc[-2]

        # Compute all indicators
        rsi = self.compute_rsi(df)
        macd, macd_signal, macd_hist = self.compute_macd(df)
        bb_upper, bb_middle, bb_lower = self.compute_bollinger_bands(df)
        atr = self.compute_atr(df)
        adx, plus_di, minus_di = self.compute_adx(df)
        stoch_k, stoch_d = self.compute_stochastic_rsi(df)
        obv = self.compute_obv(df)
        vwap = self.compute_vwap(df)
        volume_sma = self.compute_volume_sma(df)
        support, resistance = self.compute_support_resistance(df)

        # EMAs
        emas = {}
        for period in self.ema_periods:
            if len(df) >= period:
                ema = self.compute_ema(df, period)
                emas[f"ema_{period}"] = float(ema.iloc[-1])

        # Current indicator values
        current_rsi = float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else None
        current_macd = float(macd.iloc[-1]) if not np.isnan(macd.iloc[-1]) else None
        current_macd_signal = float(macd_signal.iloc[-1]) if not np.isnan(macd_signal.iloc[-1]) else None
        current_macd_hist = float(macd_hist.iloc[-1]) if not np.isnan(macd_hist.iloc[-1]) else None
        current_bb_upper = float(bb_upper.iloc[-1]) if not np.isnan(bb_upper.iloc[-1]) else None
        current_bb_middle = float(bb_middle.iloc[-1]) if not np.isnan(bb_middle.iloc[-1]) else None
        current_bb_lower = float(bb_lower.iloc[-1]) if not np.isnan(bb_lower.iloc[-1]) else None
        current_atr = float(atr.iloc[-1]) if not np.isnan(atr.iloc[-1]) else None

        # New indicators
        current_adx = float(adx.iloc[-1]) if not np.isnan(adx.iloc[-1]) else None
        current_plus_di = float(plus_di.iloc[-1]) if not np.isnan(plus_di.iloc[-1]) else None
        current_minus_di = float(minus_di.iloc[-1]) if not np.isnan(minus_di.iloc[-1]) else None
        current_stoch_k = float(stoch_k.iloc[-1]) if not np.isnan(stoch_k.iloc[-1]) else None
        current_stoch_d = float(stoch_d.iloc[-1]) if not np.isnan(stoch_d.iloc[-1]) else None
        current_obv = float(obv.iloc[-1]) if not np.isnan(obv.iloc[-1]) else None
        prev_obv = float(obv.iloc[-2]) if len(obv) >= 2 and not np.isnan(obv.iloc[-2]) else None
        current_vwap = float(vwap.iloc[-1]) if not np.isnan(vwap.iloc[-1]) else None

        # Current volume analysis
        current_volume = float(df["volume"].iloc[-1])
        avg_volume = float(volume_sma.iloc[-1]) if not np.isnan(volume_sma.iloc[-1]) else current_volume
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

        # Generate signals
        rsi_signal = self._interpret_rsi(current_rsi)
        macd_signal_text = self._interpret_macd(current_macd, current_macd_signal, current_macd_hist)
        bb_signal = self._interpret_bollinger(current_price, current_bb_upper, current_bb_lower, current_bb_middle)
        ema_signal = self._interpret_ema(current_price, emas)
        volume_signal = self._interpret_volume(volume_ratio)
        adx_signal = self._interpret_adx(current_adx, current_plus_di, current_minus_di)
        stoch_rsi_signal = self._interpret_stochastic_rsi(current_stoch_k, current_stoch_d)
        obv_signal = self._interpret_obv(current_obv, prev_obv, current_price, prev_price)
        vwap_signal = self._interpret_vwap(current_price, current_vwap)

        # Price changes
        price_change_1 = ((current_price - prev_price) / prev_price) if prev_price else 0
        if len(df) >= 24:
            price_24h_ago = df["close"].iloc[-24]
            price_change_24 = ((current_price - price_24h_ago) / price_24h_ago) if price_24h_ago else 0
        else:
            price_change_24 = 0

        result = {
            "current_price": float(current_price),
            "candle_count": len(df),
            "indicators": {
                "rsi": current_rsi,
                "rsi_signal": rsi_signal,
                "macd": current_macd,
                "macd_signal_line": current_macd_signal,
                "macd_histogram": current_macd_hist,
                "macd_signal": macd_signal_text,
                "bb_upper": current_bb_upper,
                "bb_middle": current_bb_middle,
                "bb_lower": current_bb_lower,
                "bb_signal": bb_signal,
                "atr": current_atr,
                "adx": current_adx,
                "plus_di": current_plus_di,
                "minus_di": current_minus_di,
                "adx_signal": adx_signal,
                "stoch_rsi_k": current_stoch_k,
                "stoch_rsi_d": current_stoch_d,
                "stoch_rsi_signal": stoch_rsi_signal,
                "obv": current_obv,
                "obv_signal": obv_signal,
                "vwap": current_vwap,
                "vwap_signal": vwap_signal,
                **emas,
                "ema_signal": ema_signal,
                "volume": current_volume,
                "volume_avg": avg_volume,
                "volume_ratio": volume_ratio,
                "volume_signal": volume_signal,
                "support": float(support),
                "resistance": float(resistance),
            },
            "price_changes": {
                "1h": price_change_1,
                "24h": price_change_24,
            },
        }

        logger.debug(
            f"Technical analysis complete | Price: ${current_price:,.2f} | "
            f"RSI: {current_rsi if current_rsi is not None else 0:.1f} ({rsi_signal}) | "
            f"MACD: {macd_signal_text} | BB: {bb_signal}"
        )

        return result

    # =========================================================================
    # Signal Interpretation
    # =========================================================================

    def _interpret_rsi(self, rsi: Optional[float]) -> str:
        if rsi is None:
            return "unknown"
        if rsi >= self.rsi_overbought:
            return "overbought"
        if rsi <= self.rsi_oversold:
            return "oversold"
        if rsi >= 60:
            return "bullish"
        if rsi <= 40:
            return "bearish"
        return "neutral"

    def _interpret_macd(
        self,
        macd: Optional[float],
        signal: Optional[float],
        histogram: Optional[float],
    ) -> str:
        if macd is None or signal is None:
            return "unknown"
        if macd > signal and histogram and histogram > 0:
            return "bullish"
        if macd < signal and histogram and histogram < 0:
            return "bearish"
        if macd > signal:
            return "weakly_bullish"
        if macd < signal:
            return "weakly_bearish"
        return "neutral"

    def _interpret_bollinger(
        self,
        price: float,
        upper: Optional[float],
        lower: Optional[float],
        middle: Optional[float],
    ) -> str:
        if upper is None or lower is None:
            return "unknown"
        if price >= upper:
            return "overbought"
        if price <= lower:
            return "oversold"
        if middle and price > middle:
            return "above_middle"
        if middle and price < middle:
            return "below_middle"
        return "neutral"

    def _interpret_ema(self, price: float, emas: dict) -> str:
        if not emas:
            return "unknown"

        above_count = sum(1 for _, v in emas.items() if price > v)
        total = len(emas)

        if above_count == total:
            return "strongly_bullish"
        if above_count == 0:
            return "strongly_bearish"
        if above_count >= total * 0.75:
            return "bullish"
        if above_count <= total * 0.25:
            return "bearish"
        return "neutral"

    def _interpret_volume(self, volume_ratio: float) -> str:
        if volume_ratio > 2.0:
            return "very_high"
        if volume_ratio > 1.5:
            return "high"
        if volume_ratio > 0.8:
            return "normal"
        if volume_ratio > 0.5:
            return "low"
        return "very_low"

    def _interpret_adx(
        self,
        adx: Optional[float],
        plus_di: Optional[float],
        minus_di: Optional[float],
    ) -> str:
        """
        Interpret ADX for trend strength and direction.
        ADX > 25 with +DI > -DI → strong uptrend
        ADX > 25 with -DI > +DI → strong downtrend
        ADX < 20 → no trend (range-bound, favor mean-reversion)
        """
        if adx is None:
            return "unknown"
        if adx >= 40:
            if plus_di and minus_di and plus_di > minus_di:
                return "strong_uptrend"
            return "strong_downtrend"
        if adx >= 25:
            if plus_di and minus_di and plus_di > minus_di:
                return "uptrend"
            return "downtrend"
        if adx >= 20:
            return "weak_trend"
        return "no_trend"

    def _interpret_stochastic_rsi(
        self,
        k: Optional[float],
        d: Optional[float],
    ) -> str:
        """
        Interpret Stochastic RSI.
        %K < 20 → oversold (buy signal in range)
        %K > 80 → overbought (sell signal in range)
        %K crossing above %D → bullish crossover
        """
        if k is None:
            return "unknown"
        if k > 80:
            return "overbought"
        if k < 20:
            return "oversold"
        if d is not None:
            if k > d:
                return "bullish_crossover"
            if k < d:
                return "bearish_crossover"
        return "neutral"

    def _interpret_obv(
        self,
        current_obv: Optional[float],
        prev_obv: Optional[float],
        current_price: float,
        prev_price: float,
    ) -> str:
        """
        Interpret OBV for volume-price divergence.
        OBV rising + price rising → confirmed uptrend
        OBV falling + price rising → bearish divergence (potential reversal)
        OBV rising + price falling → bullish divergence (potential reversal)
        """
        if current_obv is None or prev_obv is None:
            return "unknown"
        obv_rising = current_obv > prev_obv
        price_rising = current_price > prev_price

        if obv_rising and price_rising:
            return "confirmed_uptrend"
        if not obv_rising and not price_rising:
            return "confirmed_downtrend"
        if obv_rising and not price_rising:
            return "bullish_divergence"
        if not obv_rising and price_rising:
            return "bearish_divergence"
        return "neutral"

    def _interpret_vwap(self, price: float, vwap: Optional[float]) -> str:
        """
        Interpret VWAP.
        Price significantly above VWAP → expensive (overbought bias)
        Price significantly below VWAP → cheap (oversold bias)
        """
        if vwap is None or vwap <= 0:
            return "unknown"
        deviation = (price - vwap) / vwap
        if deviation > 0.02:
            return "above_vwap"
        if deviation < -0.02:
            return "below_vwap"
        return "at_vwap"
