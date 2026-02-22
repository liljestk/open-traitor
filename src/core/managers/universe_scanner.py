"""
UniverseScanner — Pair universe discovery, technical scanning, and LLM screening.

Extracted from Orchestrator for maintainability.  Takes an orchestrator reference
in its constructor (same pattern as PipelineManager / StateManager).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from src.analysis.technical import TechnicalAnalyzer
from src.strategies import EMACrossoverStrategy, BollingerReversionStrategy
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.core.orchestrator import Orchestrator

logger = get_logger("core.universe_scanner")


class UniverseScanner:
    """Handles the 3-stage pair funnel: universe refresh → tech scan → LLM screener."""

    def __init__(self, orchestrator: "Orchestrator"):
        self.orchestrator = orchestrator

    # =========================================================================
    # Stage 1: Refresh Pair Universe
    # =========================================================================

    def refresh_pair_universe(self) -> None:
        """Stage 1: Refresh full product universe from Coinbase (cached)."""
        import time
        orch = self.orchestrator
        now = time.time()
        if orch._pair_universe and (now - orch._pair_universe_ts) < orch._PAIR_UNIVERSE_TTL:
            return  # cache still fresh

        try:
            never_trade = orch.config.get("trading", {}).get("never_trade", [])
            only_trade = orch.config.get("trading", {}).get("only_trade", [])
            products = orch.exchange.discover_all_pairs_detailed(
                quote_currencies=None,  # use default
                never_trade=never_trade,
                only_trade=only_trade if only_trade else None,
                include_crypto_quotes=orch._include_crypto_quotes,
            )
            old_ids = {p["product_id"] for p in orch._pair_universe}
            new_ids = {p["product_id"] for p in products}
            added = new_ids - old_ids
            if added and orch._pair_universe:  # skip first load
                logger.info(f"🌍 Universe refresh: {len(added)} new listings: {sorted(added)[:10]}")
            orch._pair_universe = products
            orch._pair_universe_ts = now
            logger.debug(f"Universe: {len(products)} tradeable products")
        except Exception as e:
            logger.warning(f"Universe refresh failed: {e}")

    # =========================================================================
    # Stage 2: Technical Scan
    # =========================================================================

    def run_universe_scan(self) -> None:
        """Stage 2: Technical screen — pure math, zero LLM calls.

        Filters universe by volume/movement thresholds, fetches candles,
        runs TechnicalAnalyzer + strategies, computes composite score.
        """
        orch = self.orchestrator
        if not orch._pair_universe:
            logger.info("📭 Universe scan skipped: pair universe not yet loaded")
            return

        # Pre-filter by 24h volume and price movement
        candidates = []
        for p in orch._pair_universe:
            vol = float(p.get("volume_24h", 0) or 0)
            pct = abs(float(p.get("price_percentage_change_24h", 0) or 0))
            if vol >= orch._scan_volume_threshold and pct >= orch._scan_movement_threshold_pct:
                candidates.append(p)

        if not candidates:
            logger.info(
                f"📭 Universe scan: 0/{len(orch._pair_universe)} passed filters "
                f"(vol>{orch._scan_volume_threshold}, move>{orch._scan_movement_threshold_pct}%)"
            )
            return

        # Sort by volume descending, cap at 25 to limit API calls
        # (LLM screener only considers top 20, so 25 is sufficient)
        candidates.sort(key=lambda p: float(p.get("volume_24h", 0) or 0), reverse=True)
        candidates = candidates[:25]

        analyzer = TechnicalAnalyzer(
            orch.config.get("analysis", {}).get("technical", {})
        )
        ema_strategy = EMACrossoverStrategy(orch.config)
        bb_strategy = BollingerReversionStrategy(orch.config)

        scan_results: dict[str, dict] = {}
        _BATCH_SIZE = 5
        _BATCH_PAUSE = 1.5  # seconds between batches to avoid 429 storms

        for batch_idx in range(0, len(candidates), _BATCH_SIZE):
            # Pace batches to stay well under the Coinbase REST rate limit
            if batch_idx > 0:
                import time as _scan_time
                _scan_time.sleep(_BATCH_PAUSE)

            batch = candidates[batch_idx : batch_idx + _BATCH_SIZE]
            for product in batch:
                pair = product["product_id"]
                try:
                    # NOTE: get_candles() already calls _throttled_request() which
                    # acquires a rate-limiter token internally — no extra wait needed.
                    candles = orch.exchange.get_candles(pair, granularity="ONE_HOUR", limit=200)
                    if not candles or len(candles) < 30:
                        continue

                    analysis = analyzer.analyze(candles)
                    if "error" in analysis:
                        continue

                    # Run strategy signals (pure math)
                    ema_sig = ema_strategy.generate_signal(pair, candles, analysis)
                    bb_sig = bb_strategy.generate_signal(pair, candles, analysis)

                    # Composite score: combine indicators
                    indicators = analysis.get("indicators", {})
                    rsi = indicators.get("rsi")
                    adx = indicators.get("adx")
                    volume_ratio = indicators.get("volume_ratio", 1.0)
                    macd_hist = indicators.get("macd_histogram")

                    score = 0.0
                    # RSI momentum (not overbought, not oversold — sweet spot 30-65 for buys)
                    if rsi is not None:
                        if 30 <= rsi <= 45:
                            score += 0.25  # oversold bounce potential
                        elif 45 < rsi <= 65:
                            score += 0.15  # healthy momentum
                        elif rsi > 80:
                            score -= 0.2  # overbought

                    # ADX trend strength
                    if adx is not None and adx > 25:
                        score += 0.2

                    # Volume confirmation
                    if volume_ratio > 1.5:
                        score += 0.15
                    elif volume_ratio > 1.2:
                        score += 0.1

                    # MACD histogram positive
                    if macd_hist is not None and macd_hist > 0:
                        score += 0.1

                    # Strategy confidence boost
                    for sig in [ema_sig, bb_sig]:
                        if sig.action == "buy" and sig.confidence > 0.5:
                            score += 0.2 * sig.confidence

                    # Movement bonus (higher absolute % change = more opportunity)
                    pct_change = abs(float(product.get("price_percentage_change_24h", 0) or 0))
                    score += min(pct_change / 20.0, 0.15)  # cap at 15%

                    scan_results[pair] = {
                        "product": product,
                        "current_price": analysis.get("current_price"),
                        "rsi": rsi,
                        "adx": adx,
                        "volume_ratio": volume_ratio,
                        "macd_histogram": macd_hist,
                        "ema_signal": ema_sig.action,
                        "ema_confidence": ema_sig.confidence,
                        "bb_signal": bb_sig.action,
                        "bb_confidence": bb_sig.confidence,
                        "composite_score": round(score, 3),
                        "price_change_24h_pct": float(product.get("price_percentage_change_24h", 0) or 0),
                        "volume_24h": float(product.get("volume_24h", 0) or 0),
                    }
                except Exception as e:
                    logger.debug(f"Scan skip {pair}: {e}")
                    continue

        orch._scan_results = scan_results

        # Persist to StatsDB
        if scan_results:
            top_movers = sorted(
                scan_results.items(),
                key=lambda kv: kv[1]["composite_score"],
                reverse=True,
            )[:10]
            # M4: pass structured list for proper JSON serialisation
            top_movers_list = [
                {"pair": p, "score": d["composite_score"]} for p, d in top_movers
            ]
            top_movers_str = ", ".join(
                f"{p}={d['composite_score']}" for p, d in top_movers
            )
            try:
                orch.stats_db.save_scan_results(
                    universe_size=len(orch._pair_universe),
                    scanned_pairs=len(scan_results),
                    results_json=scan_results,
                    top_movers=top_movers_list,
                    summary_text=self.get_scan_summary(),
                )
            except Exception as e:
                logger.debug(f"Failed to persist scan results: {e}")

            logger.info(
                f"📊 Universe scan: {len(candidates)} candidates → "
                f"{len(scan_results)} scored | top: {top_movers_str[:120]}"
            )

    # =========================================================================
    # Stage 3: LLM Screener
    # =========================================================================

    def run_llm_screener(self) -> None:
        """Stage 3: Single LLM call to pick top-N active pairs from scan results.

        Uses ONE compact prompt with a summary table — not per-pair analysis.
        """
        import asyncio
        import re as _re
        orch = self.orchestrator
        if not orch._scan_results:
            logger.info("📭 LLM screener skipped: no scan results available (universe may be empty or API throttled)")
            return

        # Build top candidates sorted by composite score
        ranked = sorted(
            orch._scan_results.items(),
            key=lambda kv: kv[1]["composite_score"],
            reverse=True,
        )[:20]  # top 20 for LLM consideration

        if not ranked:
            return

        # Build compact table for LLM
        table_lines = ["Pair | Price | RSI | ADX | Vol24h | MACDh | EMA | BB | Score | Chg24h%"]
        table_lines.append("-" * 90)
        for pair, d in ranked:
            rsi = d.get('rsi')
            adx = d.get('adx')
            macd_h = d.get('macd_histogram')
            table_lines.append(
                f"{pair} | {d.get('current_price', '?'):.6g} | "
                f"{rsi:.1f if rsi is not None else '?'} | "
                f"{adx:.1f if adx is not None else '?'} | "
                f"{d.get('volume_24h', 0):.0f} | "
                f"{macd_h:.4f if macd_h is not None else '?'} | "
                f"{d.get('ema_signal', '?')}({d.get('ema_confidence', 0):.2f}) | "
                f"{d.get('bb_signal', '?')}({d.get('bb_confidence', 0):.2f}) | "
                f"{d.get('composite_score', 0):.3f} | "
                f"{d.get('price_change_24h_pct', 0):+.2f}%"
            )

        table_str = "\n".join(table_lines)

        # Currently held positions (must keep awareness)
        held_pairs = list(orch.state.open_positions.keys())
        held_note = f"Currently holding positions in: {', '.join(held_pairs)}" if held_pairs else "No open positions."

        # Use actual quote currency from config for accurate LLM examples
        qc = orch.config.get("trading", {}).get("quote_currency", "EUR")
        # Build example pairs from actual scan results for the LLM
        example_pairs = [p for p, _ in ranked[:3]]
        example_str = json.dumps(example_pairs) if example_pairs else f'["BTC-{qc}","ETH-{qc}","SOL-{qc}"]'

        prompt = (
            f"You are a crypto pair screener. Pick the best {orch._max_active_pairs} "
            f"pairs to actively trade from the scan results below.\n\n"
            f"SCAN RESULTS (sorted by composite score):\n{table_str}\n\n"
            f"{held_note}\n\n"
            f"RULES:\n"
            f"- Pick {orch._max_active_pairs} pairs total (can include held pairs if still strong)\n"
            f"- Prioritize: high composite score, buy signals, strong momentum, adequate volume\n"
            f"- Avoid: overbought (RSI>80), low volume, sell signals unless reversal expected\n"
            f"- If a held pair is weakening, it's OK to drop it (rotation will handle exit)\n\n"
            f"Reply with ONLY a JSON array of pair names, e.g. {example_str}\n"
            f"No explanation needed."
        )

        try:
            # C10 fix: LLMClient has no generate() method; use async chat()
            # C1 fix: use run_coroutine_threadsafe — may be called from non-loop thread
            import asyncio as _asyncio
            future = _asyncio.run_coroutine_threadsafe(
                orch.llm.chat(
                    system_prompt="You are a systematic crypto screener. Output ONLY valid JSON.",
                    user_message=prompt,
                    temperature=0.2,
                    max_tokens=200,
                ),
                orch._loop,
            )
            response = future.result(timeout=60)

            # Parse JSON array from response
            text = response.strip()
            # Extract JSON array from possible markdown wrapping
            json_match = _re.search(r'\[.*?\]', text, _re.DOTALL)
            if json_match:
                selected = json.loads(json_match.group())
                if isinstance(selected, list) and all(isinstance(s, str) for s in selected):
                    # Validate pairs exist in scan results
                    valid = [p for p in selected if p in orch._scan_results]
                    if not valid:
                        rejected = [p for p in selected if p not in orch._scan_results]
                        logger.warning(
                            f"⚠️ LLM screener selected {len(selected)} pairs but none "
                            f"matched scan results. LLM returned: {selected[:5]}. "
                            f"Scan has: {list(orch._scan_results.keys())[:5]}..."
                        )
                    if valid:
                        old_pairs = set(orch._screener_active_pairs)
                        orch._screener_active_pairs = valid[:orch._max_active_pairs]
                        new_pairs = set(orch._screener_active_pairs)

                        if old_pairs != new_pairs:
                            added = new_pairs - old_pairs
                            removed = old_pairs - new_pairs
                            changes = []
                            if added:
                                changes.append(f"+{','.join(added)}")
                            if removed:
                                changes.append(f"-{','.join(removed)}")
                            logger.info(
                                f"🎯 LLM Screener selected {len(valid)} pairs: "
                                f"{valid} | changes: {' '.join(changes)}"
                            )

                            # Persist LLM-selected pairs to DB so the dashboard watchlist can read them
                            try:
                                exchange_name = orch.config.get("trading", {}).get("exchange", "coinbase").lower()
                                # Remove old LLM follows, then add new ones
                                for rp in removed:
                                    orch.stats_db.unfollow_pair(rp, followed_by="llm")
                                for ap in added:
                                    orch.stats_db.follow_pair(ap, followed_by="llm", exchange=exchange_name)
                            except Exception as db_err:
                                logger.debug(f"Failed to persist LLM pair follows: {db_err}")

                            # Update WebSocket subscriptions
                            try:
                                if orch.ws_feed:
                                    orch.ws_feed.update_subscriptions(valid)
                            except Exception as ws_err:
                                logger.debug(f"WS subscription update failed: {ws_err}")
                        return

            logger.warning(f"LLM screener returned unparseable response: {text[:200]}")
        except Exception as e:
            logger.warning(f"LLM screener failed (non-fatal): {e}")

    # =========================================================================
    # Summary
    # =========================================================================

    def get_scan_summary(self) -> str:
        """Build human-readable summary of latest scan results for injection."""
        orch = self.orchestrator
        if not orch._scan_results:
            return "No scan results available yet."

        ranked = sorted(
            orch._scan_results.items(),
            key=lambda kv: kv[1]["composite_score"],
            reverse=True,
        )

        lines = [f"Universe: {len(orch._pair_universe)} products | Scanned: {len(orch._scan_results)}"]
        if orch._screener_active_pairs:
            lines.append(f"Active (LLM-selected): {', '.join(orch._screener_active_pairs)}")

        lines.append("Top 10 by composite score:")
        for pair, d in ranked[:10]:
            rsi = d.get('rsi')
            adx = d.get('adx')
            lines.append(
                f"  {pair}: score={d['composite_score']:.3f} "
                f"RSI={f'{rsi:.1f}' if rsi is not None else '?'} "
                f"ADX={f'{adx:.1f}' if adx is not None else '?'} "
                f"EMA={d.get('ema_signal', '?')} BB={d.get('bb_signal', '?')} "
                f"vol24h={d.get('volume_24h', 0):.0f} chg={d.get('price_change_24h_pct', 0):+.2f}%"
            )
        return "\n".join(lines)
