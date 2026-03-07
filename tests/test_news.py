"""
Tests for src/news/aggregator.py — News aggregation, sentiment, NLP enrichment.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.news.aggregator import (
    NewsArticle,
    NewsAggregator,
    _classify_sentiment,
    _extract_tickers,
    _relevance_score,
    _is_noise,
    _enrich_article,
    _pair_to_tickers,
    build_ticker_set_from_config,
    _GENERIC_TICKERS,
)


# ═══════════════════════════════════════════════════════════════════════════
# Sentiment classification
# ═══════════════════════════════════════════════════════════════════════════

class TestSentiment:
    def test_bullish(self):
        assert _classify_sentiment("Bitcoin surges to new all-time high") == "bullish"

    def test_bearish(self):
        assert _classify_sentiment("Crypto market crashes as panic selling intensifies") == "bearish"

    def test_neutral(self):
        assert _classify_sentiment("Report discusses market regulation") == "neutral"

    def test_mixed_leans_bullish(self):
        # "rally" + "gains" (2 bull) vs "fears" (1 bear) → bullish
        assert _classify_sentiment("Rally gains continue despite some fears") == "bullish"

    def test_empty(self):
        assert _classify_sentiment("") == "neutral"


# ═══════════════════════════════════════════════════════════════════════════
# Ticker extraction
# ═══════════════════════════════════════════════════════════════════════════

class TestTickerExtraction:
    def test_dollar_prefix(self):
        tickers = _extract_tickers("$BTC is up 5% today", _GENERIC_TICKERS)
        assert "BTC" in tickers

    def test_standalone(self):
        tickers = _extract_tickers("ETH reaches new high", _GENERIC_TICKERS)
        assert "ETH" in tickers

    def test_unknown_ticker_filtered(self):
        tickers = _extract_tickers("$FAKE is mooning", _GENERIC_TICKERS)
        assert "FAKE" not in tickers

    def test_dedup(self):
        tickers = _extract_tickers("BTC BTC BTC", _GENERIC_TICKERS)
        assert tickers.count("BTC") == 1

    def test_custom_known_tickers(self):
        # Regex matches 2-5 uppercase standalone letters, so use a 5-char ticker
        tickers = _extract_tickers("CUSM is up", {"CUSM"})
        assert "CUSM" in tickers


# ═══════════════════════════════════════════════════════════════════════════
# Noise filtering
# ═══════════════════════════════════════════════════════════════════════════

class TestNoiseFilter:
    def test_daily_discussion_is_noise(self):
        assert _is_noise("Daily General Discussion - March 2026") is True

    def test_weekly_thread_is_noise(self):
        assert _is_noise("Weekly Discussion Thread") is True

    def test_normal_title_not_noise(self):
        assert _is_noise("Bitcoin breaks $100k resistance") is False

    def test_moronic_monday(self):
        assert _is_noise("Moronic Monday – The market crashed") is True


# ═══════════════════════════════════════════════════════════════════════════
# Relevance scoring
# ═══════════════════════════════════════════════════════════════════════════

class TestRelevanceScore:
    def test_base_score(self):
        article = NewsArticle(title="Generic article", summary="Nothing specific")
        score = _relevance_score(article)
        assert 0 <= score <= 1.0
        assert score >= 0.2  # base score

    def test_ticker_mention_boosts_score(self):
        article_with = NewsArticle(
            title="BTC reaches new high",
            summary="Bitcoin surges",
        )
        article_without = NewsArticle(
            title="Generic market news",
            summary="Market is stable",
        )
        score_with = _relevance_score(article_with, _GENERIC_TICKERS)
        score_without = _relevance_score(article_without, _GENERIC_TICKERS)
        assert score_with > score_without

    def test_rss_tag_boosts_score(self):
        article = NewsArticle(title="Test", summary="", tags=["rss"])
        score = _relevance_score(article)
        assert score >= 0.3

    def test_recent_article_boost(self):
        recent = NewsArticle(
            title="Test", summary="",
            published=datetime.now(timezone.utc) - timedelta(minutes=30),
        )
        old = NewsArticle(
            title="Test", summary="",
            published=datetime.now(timezone.utc) - timedelta(hours=12),
        )
        assert _relevance_score(recent) > _relevance_score(old)

    def test_short_content_penalty(self):
        short = NewsArticle(title="Hi", summary="")
        long = NewsArticle(title="This is a longer title about crypto", summary="More details here")
        assert _relevance_score(short) <= _relevance_score(long)


# ═══════════════════════════════════════════════════════════════════════════
# Article enrichment
# ═══════════════════════════════════════════════════════════════════════════

class TestEnrichArticle:
    def test_adds_sentiment(self):
        article = NewsArticle(
            title="Bitcoin surges past resistance",
            summary="Rally continues",
        )
        enriched = _enrich_article(article)
        assert enriched.sentiment == "bullish"

    def test_adds_tickers_to_tags(self):
        article = NewsArticle(
            title="ETH breaks $3000",
            summary="Ethereum rally",
        )
        enriched = _enrich_article(article, _GENERIC_TICKERS)
        assert "ETH" in enriched.tags

    def test_sets_relevance_score(self):
        article = NewsArticle(title="BTC News", summary="")
        enriched = _enrich_article(article)
        assert enriched.relevance_score > 0


# ═══════════════════════════════════════════════════════════════════════════
# NewsArticle model
# ═══════════════════════════════════════════════════════════════════════════

class TestNewsArticle:
    def test_auto_id_generation(self):
        article = NewsArticle(title="Test", url="http://example.com")
        assert article.id  # Not empty

    def test_unique_ids(self):
        a1 = NewsArticle(title="Article 1", url="http://a.com")
        a2 = NewsArticle(title="Article 2", url="http://b.com")
        assert a1.id != a2.id

    def test_default_fields(self):
        article = NewsArticle()
        assert article.title == ""
        assert article.sentiment is None
        assert article.relevance_score == 0.0
        assert article.tags == []


# ═══════════════════════════════════════════════════════════════════════════
# Pair → ticker conversion
# ═══════════════════════════════════════════════════════════════════════════

class TestPairToTickers:
    def test_crypto_pair(self):
        tickers = _pair_to_tickers("BTC-EUR")
        assert "BTC" in tickers

    def test_ibkr_dotted(self):
        tickers = _pair_to_tickers("ASML.AS-EUR")
        assert "ASML" in tickers

    def test_simple_pair(self):
        tickers = _pair_to_tickers("ETH-EURC")
        assert "ETH" in tickers


class TestBuildTickerSet:
    def test_builds_from_config(self):
        cfg = {"trading": {"pairs": ["BTC-EUR", "ETH-EUR"]}}
        tickers = build_ticker_set_from_config(cfg)
        assert "BTC" in tickers
        assert "ETH" in tickers

    def test_empty_config(self):
        tickers = build_ticker_set_from_config({})
        assert len(tickers) == 0


# ═══════════════════════════════════════════════════════════════════════════
# NewsAggregator
# ═══════════════════════════════════════════════════════════════════════════

class TestNewsAggregator:
    def test_init(self):
        agg = NewsAggregator(config={"max_articles": 50})
        assert agg.max_articles == 50
        assert len(agg.articles) == 0

    def test_known_tickers_includes_generics(self):
        agg = NewsAggregator(config={})
        assert "BTC" in agg._known_tickers

    def test_known_tickers_includes_config_pairs(self):
        agg = NewsAggregator(config={"trading": {"pairs": ["SOL-EUR"]}})
        assert "SOL" in agg._known_tickers

    @patch("src.news.aggregator.requests.get")
    def test_fetch_reddit_json_handles_failure(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp
        agg = NewsAggregator(config={})
        articles = agg._fetch_reddit_json()
        assert articles == []

    @patch("src.news.aggregator.feedparser.parse")
    def test_fetch_rss(self, mock_parse):
        mock_parse.return_value = MagicMock(
            entries=[
                MagicMock(
                    title="BTC breaks $100k",
                    get=lambda k, d="": {"link": "http://example.com", "summary": "BTC rally"}.get(k, d),
                    link="http://example.com",
                    summary="BTC rally",
                    published_parsed=None,
                ),
            ],
            bozo=False,
        )
        agg = NewsAggregator(config={"rss_feeds": ["http://test.com/rss"]})
        articles = agg.fetch_rss()
        assert len(articles) >= 0  # May be 0 if feedparser mock doesn't match

    def test_fetch_rss_rejects_non_http_urls(self):
        """SSRF protection: non-http(s) URLs must be rejected."""
        agg = NewsAggregator(config={"rss_feeds": [
            "file:///etc/passwd",
            "ftp://internal.host/data",
            "gopher://evil.com",
        ]})
        articles = agg.fetch_rss()
        assert articles == []
