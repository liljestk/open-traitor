"""Tests for TechnicalAnalyzer — indicators and signal interpretation."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.technical import TechnicalAnalyzer


@pytest.fixture
def analyzer():
    return TechnicalAnalyzer()


def _make_candles(n=50, start_price=100.0, trend=0.0, volatility=2.0):
    """Generate synthetic OHLCV candles for testing."""
    candles = []
    price = start_price
    for i in range(n):
        price += trend + np.random.uniform(-volatility, volatility)
        price = max(price, 1.0)
        o = price + np.random.uniform(-1, 1)
        h = max(o, price) + np.random.uniform(0, volatility)
        l = min(o, price) - np.random.uniform(0, volatility)
        l = max(l, 0.01)
        v = np.random.uniform(100, 10000)
        candles.append({
            "start": str(1_000_000 + i * 3600),
            "open": str(o), "high": str(h), "low": str(l),
            "close": str(price), "volume": str(v),
        })
    return candles


def _make_trending_candles(n=100, start=100.0, trend=1.0):
    """Generate clearly trending candles."""
    return _make_candles(n, start_price=start, trend=trend, volatility=0.5)


# ═══════════════════════════════════════════════════════════════════════
# DataFrame Conversion
# ═══════════════════════════════════════════════════════════════════════

class TestCandlesToDataframe:
    def test_empty(self, analyzer):
        df = analyzer.candles_to_dataframe([])
        assert df.empty

    def test_basic_conversion(self, analyzer):
        candles = _make_candles(10)
        df = analyzer.candles_to_dataframe(candles)
        assert len(df) == 10
        assert "close" in df.columns
        assert df["close"].dtype == np.float64

    def test_sorted_by_timestamp(self, analyzer):
        candles = _make_candles(20)
        # Shuffle
        np.random.shuffle(candles)
        df = analyzer.candles_to_dataframe(candles)
        timestamps = df["timestamp"].tolist()
        assert timestamps == sorted(timestamps)


# ═══════════════════════════════════════════════════════════════════════
# RSI
# ═══════════════════════════════════════════════════════════════════════

class TestRSI:
    def test_rsi_range(self, analyzer):
        candles = _make_candles(50)
        df = analyzer.candles_to_dataframe(candles)
        rsi = analyzer.compute_rsi(df)
        valid = rsi.dropna()
        assert all(0 <= v <= 100 for v in valid)

    def test_rsi_uptrend_is_high(self, analyzer):
        candles = _make_trending_candles(100, trend=2.0)
        df = analyzer.candles_to_dataframe(candles)
        rsi = analyzer.compute_rsi(df)
        # In strong uptrend, RSI should be high
        assert rsi.iloc[-1] > 50

    def test_interpret_rsi_overbought(self, analyzer):
        assert analyzer._interpret_rsi(80) == "overbought"

    def test_interpret_rsi_oversold(self, analyzer):
        assert analyzer._interpret_rsi(20) == "oversold"

    def test_interpret_rsi_neutral(self, analyzer):
        assert analyzer._interpret_rsi(50) == "neutral"

    def test_interpret_rsi_none(self, analyzer):
        assert analyzer._interpret_rsi(None) == "unknown"

    def test_interpret_rsi_bullish(self, analyzer):
        assert analyzer._interpret_rsi(65) == "bullish"

    def test_interpret_rsi_bearish(self, analyzer):
        assert analyzer._interpret_rsi(35) == "bearish"


# ═══════════════════════════════════════════════════════════════════════
# MACD
# ═══════════════════════════════════════════════════════════════════════

class TestMACD:
    def test_macd_returns_three_series(self, analyzer):
        candles = _make_candles(50)
        df = analyzer.candles_to_dataframe(candles)
        macd, signal, hist = analyzer.compute_macd(df)
        assert len(macd) == len(df)
        assert len(signal) == len(df)
        assert len(hist) == len(df)

    def test_interpret_macd_bullish(self, analyzer):
        assert analyzer._interpret_macd(1.0, 0.5, 0.5) == "bullish"

    def test_interpret_macd_bearish(self, analyzer):
        assert analyzer._interpret_macd(-1.0, -0.5, -0.5) == "bearish"

    def test_interpret_macd_unknown(self, analyzer):
        assert analyzer._interpret_macd(None, None, None) == "unknown"


# ═══════════════════════════════════════════════════════════════════════
# Bollinger Bands
# ═══════════════════════════════════════════════════════════════════════

class TestBollingerBands:
    def test_bands_ordering(self, analyzer):
        candles = _make_candles(50)
        df = analyzer.candles_to_dataframe(candles)
        upper, middle, lower = analyzer.compute_bollinger_bands(df)
        valid_idx = upper.dropna().index
        for i in valid_idx:
            assert upper[i] >= middle[i] >= lower[i]

    def test_interpret_bollinger_overbought(self, analyzer):
        assert analyzer._interpret_bollinger(110, 105, 95, 100) == "overbought"

    def test_interpret_bollinger_oversold(self, analyzer):
        assert analyzer._interpret_bollinger(90, 105, 95, 100) == "oversold"

    def test_interpret_bollinger_unknown(self, analyzer):
        assert analyzer._interpret_bollinger(100, None, None, None) == "unknown"


# ═══════════════════════════════════════════════════════════════════════
# EMA / SMA
# ═══════════════════════════════════════════════════════════════════════

class TestEMA:
    def test_ema_length(self, analyzer):
        candles = _make_candles(50)
        df = analyzer.candles_to_dataframe(candles)
        ema = analyzer.compute_ema(df, 9)
        assert len(ema) == len(df)

    def test_interpret_ema_all_above(self, analyzer):
        assert analyzer._interpret_ema(200, {"ema_9": 100, "ema_21": 150}) == "strongly_bullish"

    def test_interpret_ema_all_below(self, analyzer):
        assert analyzer._interpret_ema(50, {"ema_9": 100, "ema_21": 150}) == "strongly_bearish"

    def test_interpret_ema_empty(self, analyzer):
        assert analyzer._interpret_ema(100, {}) == "unknown"


# ═══════════════════════════════════════════════════════════════════════
# ATR
# ═══════════════════════════════════════════════════════════════════════

class TestATR:
    def test_atr_positive(self, analyzer):
        candles = _make_candles(50)
        df = analyzer.candles_to_dataframe(candles)
        atr = analyzer.compute_atr(df)
        valid = atr.dropna()
        assert all(v > 0 for v in valid)


# ═══════════════════════════════════════════════════════════════════════
# ADX
# ═══════════════════════════════════════════════════════════════════════

class TestADX:
    def test_adx_range(self, analyzer):
        candles = _make_candles(60)
        df = analyzer.candles_to_dataframe(candles)
        adx, plus_di, minus_di = analyzer.compute_adx(df)
        valid = adx.dropna()
        assert all(v >= 0 for v in valid)

    def test_interpret_adx_strong_uptrend(self, analyzer):
        assert analyzer._interpret_adx(45.0, 30.0, 10.0) == "strong_uptrend"

    def test_interpret_adx_no_trend(self, analyzer):
        assert analyzer._interpret_adx(15.0, 20.0, 18.0) == "no_trend"

    def test_interpret_adx_unknown(self, analyzer):
        assert analyzer._interpret_adx(None, None, None) == "unknown"


# ═══════════════════════════════════════════════════════════════════════
# Stochastic RSI
# ═══════════════════════════════════════════════════════════════════════

class TestStochasticRSI:
    def test_stoch_rsi_range(self, analyzer):
        candles = _make_candles(60)
        df = analyzer.candles_to_dataframe(candles)
        k, d = analyzer.compute_stochastic_rsi(df)
        valid_k = k.dropna()
        assert all(0 <= v <= 100 for v in valid_k)

    def test_interpret_overbought(self, analyzer):
        assert analyzer._interpret_stochastic_rsi(85.0, 70.0) == "overbought"

    def test_interpret_oversold(self, analyzer):
        assert analyzer._interpret_stochastic_rsi(15.0, 20.0) == "oversold"


# ═══════════════════════════════════════════════════════════════════════
# OBV
# ═══════════════════════════════════════════════════════════════════════

class TestOBV:
    def test_obv_length(self, analyzer):
        candles = _make_candles(50)
        df = analyzer.candles_to_dataframe(candles)
        obv = analyzer.compute_obv(df)
        assert len(obv) == len(df)

    def test_interpret_confirmed_uptrend(self, analyzer):
        assert analyzer._interpret_obv(200, 100, 55, 50) == "confirmed_uptrend"

    def test_interpret_bearish_divergence(self, analyzer):
        assert analyzer._interpret_obv(100, 200, 55, 50) == "bearish_divergence"


# ═══════════════════════════════════════════════════════════════════════
# VWAP
# ═══════════════════════════════════════════════════════════════════════

class TestVWAP:
    def test_vwap_positive(self, analyzer):
        candles = _make_candles(50)
        df = analyzer.candles_to_dataframe(candles)
        vwap = analyzer.compute_vwap(df)
        valid = vwap.dropna()
        assert all(v > 0 for v in valid)

    def test_interpret_above(self, analyzer):
        assert analyzer._interpret_vwap(103, 100) == "above_vwap"

    def test_interpret_below(self, analyzer):
        assert analyzer._interpret_vwap(97, 100) == "below_vwap"

    def test_interpret_at(self, analyzer):
        assert analyzer._interpret_vwap(100.5, 100) == "at_vwap"


# ═══════════════════════════════════════════════════════════════════════
# Volume
# ═══════════════════════════════════════════════════════════════════════

class TestVolume:
    def test_interpret_very_high(self, analyzer):
        assert analyzer._interpret_volume(2.5) == "very_high"

    def test_interpret_high(self, analyzer):
        assert analyzer._interpret_volume(1.6) == "high"

    def test_interpret_normal(self, analyzer):
        assert analyzer._interpret_volume(1.0) == "normal"

    def test_interpret_low(self, analyzer):
        assert analyzer._interpret_volume(0.6) == "low"

    def test_interpret_very_low(self, analyzer):
        assert analyzer._interpret_volume(0.3) == "very_low"


# ═══════════════════════════════════════════════════════════════════════
# Support/Resistance
# ═══════════════════════════════════════════════════════════════════════

class TestSupportResistance:
    def test_support_below_resistance(self, analyzer):
        candles = _make_candles(50)
        df = analyzer.candles_to_dataframe(candles)
        support, resistance = analyzer.compute_support_resistance(df)
        assert support <= resistance


# ═══════════════════════════════════════════════════════════════════════
# Full Analyze
# ═══════════════════════════════════════════════════════════════════════

class TestFullAnalyze:
    def test_insufficient_data(self, analyzer):
        result = analyzer.analyze(_make_candles(5))
        assert "error" in result

    def test_full_analysis_keys(self, analyzer):
        result = analyzer.analyze(_make_candles(100))
        assert "current_price" in result
        assert "indicators" in result
        assert "price_changes" in result
        ind = result["indicators"]
        assert "rsi" in ind
        assert "macd" in ind
        assert "bb_upper" in ind
        assert "adx" in ind
        assert "stoch_rsi_k" in ind
        assert "obv" in ind
        assert "vwap" in ind
        assert "support" in ind
        assert "resistance" in ind

    def test_custom_config(self):
        custom = TechnicalAnalyzer(config={"rsi_period": 7, "rsi_overbought": 80, "rsi_oversold": 20})
        assert custom.rsi_period == 7
        assert custom.rsi_overbought == 80
        assert custom.rsi_oversold == 20
