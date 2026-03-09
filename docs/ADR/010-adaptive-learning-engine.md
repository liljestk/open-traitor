# ADR-010: Adaptive Learning Engine (ALE)

**Status:** Accepted

## Context

Static trading parameters degrade over time as market conditions change. Manual tuning is labor-intensive and reactive. The system needs a structured way to adapt — adjusting confidence calibration, strategy weights, prompt quality, and technical parameters — without human intervention, while maintaining a global kill switch for safety.

## Decision

Implement an **Adaptive Learning Engine (ALE)** with six independent subsystems running on staggered schedules, controlled by a global enable/disable switch.

### Global Kill Switch

```python
async def tick(self, cycle_count: int = 0) -> dict[str, Any]:
    settings = llm_optimizer.get_settings()
    if not settings.get("learning_enabled", True):
        return {"skipped": True, "reason": "learning_disabled"}
    # run subsystems
```

When disabled, all subsystems are halted immediately. This is a single toggle accessible from the dashboard settings.

### Subsystems

| Subsystem | Schedule | Purpose |
|-----------|----------|---------|
| **SignalScorecard** | Every cycle | Score buy/sell/hold signals against realized prices; lightweight SQL |
| **ConfidenceCalibrator** | Weekly | Retrain probability calibration so confidence scores reflect real win rates |
| **EnsembleOptimizer** | Weekly | Update strategy weights based on recent risk-adjusted P&L |
| **PromptEvolver** | Weekly | Analyze prediction patterns → generate prompt supplements (few-shot examples, domain insights) |
| **AutoWFO** | Weekly | Walk-Forward Optimization: parameter search (TP%, SL%) on rolling windows per pair |
| **FinetuningPipeline** | Monthly | Export reasoning samples → prepare training data for Ollama fine-tune |

### Schedule Constants

```python
_SCORECARD_INTERVAL = 0                     # every cycle
_CALIBRATOR_INTERVAL = 7 * 24 * 3600       # weekly
_ENSEMBLE_INTERVAL = 7 * 24 * 3600         # weekly
_PROMPT_EVOLVER_INTERVAL = 7 * 24 * 3600   # weekly
_AUTO_WFO_INTERVAL = 7 * 24 * 3600         # weekly
_FINETUNE_INTERVAL = 30 * 24 * 3600        # monthly
```

### Subsystem Details

**SignalScorecard**: Runs every cycle (near-zero cost). Compares predicted signals to actual price movements. Feeds accuracy metrics to the ConfidenceCalibrator.

**ConfidenceCalibrator**: Weekly recalibration ensures that when the system reports 80% confidence, it's correct ~80% of the time. Input: signal scores + outcomes.

**EnsembleOptimizer**: Adjusts strategy weights, favoring strategies with better risk-adjusted returns. Maximum shift per update capped at 5% (`ensemble_max_shift: 0.05`).

**PromptEvolver**: Identifies patterns in correct/incorrect predictions and generates targeted prompt supplements injected into agent system prompts. Capped at 500 tokens (`prompt_supplement_max_tokens: 500`).

**AutoWFO**: Walk-Forward Optimization searches parameter space on rolling historical windows. Only accepts configurations with Walk-Forward Efficiency ≥ 0.5 (`wfo_min_wfe: 0.5`).

**FinetuningPipeline**: Monthly export of agent reasoning samples for Ollama fine-tuning. Requires minimum 50 examples (`finetune_min_examples: 50`).

### Persistence

- `learning_runs` table tracks each subsystem execution (cycle, duration, status, result).
- Each subsystem has dedicated persistence (signal_scores, calibrator_state, ensemble_weights, etc.).
- `data/llm_optimizer_settings.json` stores the current ALE configuration.

### Configuration Defaults

```python
DEFAULTS = {
    "learning_enabled": True,
    "calibration_min_samples": 50,
    "ensemble_max_shift": 0.05,
    "prompt_supplement_max_tokens": 500,
    "wfo_min_wfe": 0.5,
    "finetune_min_examples": 50,
}
```

## Consequences

**Benefits:**
- Continuous adaptation to changing market conditions without manual intervention.
- Each subsystem is independent; failure in one doesn't affect others.
- Global kill switch provides instant safety stop.
- Weekly cadence balances responsiveness with signal-to-noise ratio.
- Gradual parameter shifts (`max_shift: 0.05`) prevent catastrophic model swings.

**Risks:**
- Overfitting: AutoWFO and PromptEvolver could tune to recent noise. Mitigated by WFE threshold and token caps.
- Cascading feedback: calibrator uses scorecard output, which uses strategy weights from the optimizer. Staggered schedules reduce coupling.

**Trade-offs:**
- Weekly cadence means the system takes 1–4 weeks to adapt to new regimes. This is intentional — faster adaptation risks chasing noise.
- Monthly fine-tuning is expensive (compute) but infrequent enough to be manageable.
