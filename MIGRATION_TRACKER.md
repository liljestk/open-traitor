# Autonomous Super-Trader Migration Tracker

**Goal:** LLMs act as autonomous super-traders operating a deterministic toolkit. Deterministic core enforces guardrails (edge library, allocator, AbsoluteRules) the LLM cannot bypass. No paper-mode shortcuts.

**Status legend:** `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked

---

## 1. Deterministic Decision Engine ([src/core/decision_engine.py](src/core/decision_engine.py))
- [x] Module created with `DecisionEngine`, `TradeProposal`, `DecisionVerdict`
- [x] Edge-library veto: vetoes buy when `n_samples >= min_edge_samples` AND `sharpe < min_edge_sharpe`
- [x] Allocator-aware sizing budget: `allocator.weights()[strategy] * portfolio_value`
- [x] Ensemble agreement gate: solitary contrarian buy requires pattern confirmation
- [x] AbsoluteRules pass-through (advisory pre-flight)
- [x] Returns structured verdict with reasons (full LLM observability)

## 2. Trading Toolkit ([src/core/trading_toolkit.py](src/core/trading_toolkit.py))
- [x] `TradingToolkit` class — structured tool surface for the LLM trader
- [x] Tools: `get_market_snapshot`, `get_strategy_signals`, `get_pattern_signal`, `get_edge_stats`, `get_allocator_weights`, `get_portfolio_state`, `propose_trade`
- [x] Only `propose_trade` is side-effecting (calls DecisionEngine)
- [x] OpenAI-compatible JSON tool schemas via `get_tool_schemas()`

## 3. TraderAgent ([src/agents/trader.py](src/agents/trader.py))
- [x] LLM-driven autonomous trader operating the toolkit
- [x] `LLMClient.chat_json` based with deterministic fallback when LLM is unreachable
- [x] Replaces `StrategistAgent` in the live decision path (Strategist kept as advisory sidecar)
- [x] Reasoning persisted via `stats_db.save_reasoning(agent_name="trader", ...)`
- [x] Single retry on engine veto with rejection feedback in the next prompt

## 4. LLMAdvisor + ShadowTester wiring
- [x] Instantiated in `Orchestrator.__init__` with per-profile state path
- [x] Exposed as `orch.advisor`, `orch.shadow_tester`
- [x] Parameter delta path: `settings_advisor` → `shadow_tester.propose()` → orchestrator promotion consumer
- [ ] News classification path: `EventManager` calls `advisor.classify_news` *(future hook; advisor reachable but not auto-invoked)*
- [ ] Postmortem path: losing trades trigger `advisor.write_postmortem` *(future hook)*

## 5. Allocator-driven sizing ([src/agents/risk_manager.py](src/agents/risk_manager.py))
- [x] Step 4d: honour `proposal["allocator_budget_cap"]` written by `DecisionEngine`
- [x] Fallback: read `context["quant"].allocator.weights()` for legacy strategist proposals
- [x] Pipeline now passes `"quant": orch.quant` into `risk_manager.execute(...)` context
- [x] Test: [tests/test_allocator_sizing.py](tests/test_allocator_sizing.py)

## 6. Edge-library gating
- [x] DecisionEngine queries `edges.edge()` before approval and vetoes when stats are conclusive
- [x] Pipeline already records `record_signal_sample` after fills via the executor
- [x] Pre-loaded losing history → backtest harness confirms `no_edge` veto fires

## 7. Pattern agent → first-class ensemble
- [x] Pattern signal injected into `strategy_signals["pattern_engine"]` and re-fed through `_compute_ensemble`
- [x] `_STRATEGY_WEIGHTS` updated: `ema_crossover` 0.45 / `bollinger_reversion` 0.35 / `pattern_engine` 0.20
- [x] Allocator pre-registers pattern_engine via `quant.register_strategies([..., "pattern_engine"])`

## 8. Settings advisor → ShadowTester
- [x] `SettingsAdvisorAgent` proposes via `context["shadow_tester"].propose(...)` instead of direct YAML write
- [x] Orchestrator `_apply_promotable_shadow_deltas()` runs every cycle; consumes `list_promotable()`, re-validates, then `sm.update_section`
- [x] Promoted deltas pushed to runtime via `sm.push_section_to_runtime`; `shadow_pending` entries skipped
- [x] Existing backtest gate retained as a secondary check before the proposal enters the shadow queue

## 9. Pipeline switch
- [x] `pipeline_manager.run_pipeline` invokes `orch.trader.execute(...)` after the strategist call
- [x] If trader returns a non-error result it replaces `strategy_result`; otherwise strategist output is used
- [x] DecisionEngine called inside TraderAgent — its verdict travels with the proposal into `risk_manager`
- [x] Hard-veto returns `action="hold"`; soft-veto downsizes via `allocator_budget_cap`

## 10. Backtest harness for deterministic path
- [x] New [src/backtesting/decision_engine_backtest.py](src/backtesting/decision_engine_backtest.py)
- [x] Replays candles through DecisionEngine with rule-based responder (LLM-free)
- [x] Records edge samples → updates `InMemorySignalEdgeStore` → confirms gating fires
- [x] Smoke test in [tests/test_decision_engine_backtest.py](tests/test_decision_engine_backtest.py)

## 11. Tests
- [x] [tests/test_decision_engine.py](tests/test_decision_engine.py) — verdict logic for every gate (7 tests)
- [x] [tests/test_trader_agent.py](tests/test_trader_agent.py) — toolkit invocation + LLM-down fallback (3 tests)
- [x] [tests/test_shadow_tester_integration.py](tests/test_shadow_tester_integration.py) — propose → observe → promotable → consume (2 tests)
- [x] [tests/test_decision_engine_backtest.py](tests/test_decision_engine_backtest.py) — losing history triggers `no_edge` veto (2 tests)
- [x] [tests/test_allocator_sizing.py](tests/test_allocator_sizing.py) — `allocator_budget_cap` enforced (2 tests)
- [x] Existing risk-manager tests still green (16 passed locally)

## 12. Cleanup
- [x] [src/agents/__init__.py](src/agents/__init__.py) exports `TraderAgent`
- [x] [src/core/__init__.py](src/core/__init__.py) exports `DecisionEngine`, `DecisionVerdict`, `TradeProposal`, `TradingToolkit`, `ToolkitContext`
- [x] No paper-mode fallbacks added
- [x] No feature flag escape hatch — trader output wins when available

## 13. Domain separation guards
- [x] ShadowTester state file: `data/<profile>/shadow_state.json` (profile from `AUTO_TRAITOR_PROFILE` / exchange class)
- [x] No new Redis keys introduced
- [x] No new SQL queries — reasoning persisted via existing `stats_db.save_reasoning(..., exchange=...)`
- [x] Frontend untouched

## 14. Commit + push
- [x] Logical commits (decision-engine + toolkit + trader + wiring + risk-cap + settings-shadow + tests + tracker)
- [ ] Pushed to remote *(awaiting user approval per `.github/copilot-instructions.md`)*
