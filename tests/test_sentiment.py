"""
Tests for SentimentAnalyzer — keyword-based crypto sentiment scoring.
"""

import pytest

from src.analysis.sentiment import SentimentAnalyzer, SentimentResult, AggregateSentiment


@pytest.fixture
def analyzer():
    return SentimentAnalyzer()


# ═══════════════════════════════════════════════════════════════════════════
# Single text analysis
# ═══════════════════════════════════════════════════════════════════════════

class TestAnalyzeText:
    def test_empty_text_returns_neutral(self, analyzer):
        r = analyzer.analyze_text("")
        assert r.score == 0.0
        assert r.label == "neutral"
        assert r.confidence == 0.0

    def test_strong_bullish(self, analyzer):
        r = analyzer.analyze_text("Bitcoin ETF approved! All-time high breakout incoming, parabolic rally!")
        assert r.score > 0.3
        assert r.label in ("bullish", "very_bullish")
        assert len(r.bullish_matches) > 0

    def test_strong_bearish(self, analyzer):
        r = analyzer.analyze_text("Crypto crash incoming — SEC lawsuit, rug pull confirmed, capitulation!")
        assert r.score < -0.3
        assert r.label in ("bearish", "very_bearish")
        assert len(r.bearish_matches) > 0

    def test_neutral_no_keywords(self, analyzer):
        r = analyzer.analyze_text("The sky is blue, time for lunch.")
        assert r.score == 0.0
        assert r.label == "neutral"

    def test_mixed_sentiment(self, analyzer):
        r = analyzer.analyze_text("Bitcoin rally amid SEC lawsuit concerns and fear")
        assert len(r.bullish_matches) > 0
        assert len(r.bearish_matches) > 0

    def test_source_preserved(self, analyzer):
        r = analyzer.analyze_text("bullish breakout", source="CoinDesk")
        assert r.source == "CoinDesk"

    def test_timestamp_present(self, analyzer):
        r = analyzer.analyze_text("bullish")
        assert r.timestamp

    def test_score_bounds(self, analyzer):
        # Even with many keywords, score stays in [-1, 1]
        text = " ".join(["breakout all-time high moon parabolic rally surge soaring etf approved"] * 5)
        r = analyzer.analyze_text(text)
        assert -1.0 <= r.score <= 1.0

    def test_to_dict(self, analyzer):
        r = analyzer.analyze_text("bullish breakout", source="test")
        d = r.to_dict()
        assert "score" in d
        assert "label" in d
        assert "confidence" in d
        assert "bullish_matches" in d
        assert "bearish_matches" in d


# ═══════════════════════════════════════════════════════════════════════════
# Score labels
# ═══════════════════════════════════════════════════════════════════════════

class TestScoreLabels:
    def test_very_bullish(self, analyzer):
        assert analyzer._score_label(0.6) == "very_bullish"

    def test_bullish(self, analyzer):
        assert analyzer._score_label(0.2) == "bullish"

    def test_neutral(self, analyzer):
        assert analyzer._score_label(0.0) == "neutral"
        assert analyzer._score_label(0.1) == "neutral"
        assert analyzer._score_label(-0.1) == "neutral"

    def test_bearish(self, analyzer):
        assert analyzer._score_label(-0.2) == "bearish"

    def test_very_bearish(self, analyzer):
        assert analyzer._score_label(-0.6) == "very_bearish"


# ═══════════════════════════════════════════════════════════════════════════
# Batch analysis
# ═══════════════════════════════════════════════════════════════════════════

class TestAnalyzeBatch:
    def test_empty_batch(self, analyzer):
        agg = analyzer.analyze_batch([])
        assert agg.total_sources == 0
        assert agg.label == "neutral"

    def test_all_bullish(self, analyzer):
        items = [
            {"title": "ETF approved! Breakout confirmed"},
            {"title": "Institutional buying surge, parabolic move"},
        ]
        agg = analyzer.analyze_batch(items)
        assert agg.bullish_count == 2
        assert agg.bearish_count == 0
        assert agg.weighted_score > 0

    def test_all_bearish(self, analyzer):
        items = [
            {"title": "Crash imminent, rug pull confirmed"},
            {"title": "SEC lawsuit, capitulation and panic selling"},
        ]
        agg = analyzer.analyze_batch(items)
        assert agg.bearish_count == 2
        assert agg.weighted_score < 0

    def test_mixed_batch(self, analyzer):
        items = [
            {"title": "Bitcoin rally to all-time high"},
            {"title": "Major crash expected, bear market confirmed"},
            {"title": "Normal market activity today"},
        ]
        agg = analyzer.analyze_batch(items)
        assert agg.total_sources == 3
        assert agg.bullish_count >= 1
        assert agg.bearish_count >= 1

    def test_recency_weighting(self, analyzer):
        # First item (most recent) is bullish, second is bearish
        items = [
            {"title": "ETF approved! Breakout parabolic rally!"},
            {"title": "Crash rug pull capitulation dump"},
        ]
        agg = analyzer.analyze_batch(items)
        # Recency-weighted should favor the first (bullish) item
        assert agg.weighted_score > agg.mean_score or abs(agg.weighted_score - agg.mean_score) < 0.5

    def test_extra_text_keys(self, analyzer):
        items = [
            {"title": "Market update", "body": "Massive breakout rally confirmed"},
        ]
        agg = analyzer.analyze_batch(items)
        assert agg.bullish_count >= 1

    def test_top_keywords_populated(self, analyzer):
        items = [
            {"title": "breakout rally breakout surge"},
            {"title": "breakout again, more rally"},
        ]
        agg = analyzer.analyze_batch(items)
        assert len(agg.top_bullish) > 0


# ═══════════════════════════════════════════════════════════════════════════
# Pair-specific scoring
# ═══════════════════════════════════════════════════════════════════════════

class TestScoreForPair:
    def test_filters_by_pair(self, analyzer):
        items = [
            {"title": "Bitcoin rally to new high"},
            {"title": "Ethereum crash imminent"},
        ]
        btc = analyzer.score_for_pair("BTC-EUR", items)
        eth = analyzer.score_for_pair("ETH-EUR", items)
        assert btc["sentiment_score"] > eth["sentiment_score"]

    def test_no_matching_articles(self, analyzer):
        items = [{"title": "Nothing about crypto here"}]
        result = analyzer.score_for_pair("SOL-USD", items)
        assert result["total_articles"] == 0
        assert result["sentiment_label"] == "neutral"

    def test_name_mapping(self, analyzer):
        items = [{"title": "Solana breakout rally confirmed!"}]
        result = analyzer.score_for_pair("SOL-USD", items)
        assert result["total_articles"] == 1
        assert result["sentiment_score"] > 0

    def test_result_dict_keys(self, analyzer):
        items = [{"title": "Bitcoin bullish"}]
        result = analyzer.score_for_pair("BTC-USD", items)
        expected_keys = {
            "sentiment_score", "sentiment_label", "bullish_count",
            "bearish_count", "neutral_count", "total_articles", "top_keywords",
        }
        assert expected_keys.issubset(result.keys())


# ═══════════════════════════════════════════════════════════════════════════
# Custom keywords via config
# ═══════════════════════════════════════════════════════════════════════════

class TestCustomKeywords:
    def test_custom_bullish_keyword(self):
        analyzer = SentimentAnalyzer({"sentiment": {"custom_bullish": {"wagmi": 0.9}}})
        r = analyzer.analyze_text("wagmi is the vibe today")
        assert r.score > 0
        assert "wagmi" in r.bullish_matches

    def test_custom_bearish_keyword(self):
        analyzer = SentimentAnalyzer({"sentiment": {"custom_bearish": {"ngmi": 0.9}}})
        r = analyzer.analyze_text("ngmi if you hold this")
        assert r.score < 0
        assert "ngmi" in r.bearish_matches
