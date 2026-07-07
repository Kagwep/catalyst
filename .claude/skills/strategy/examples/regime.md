# Example: regime-detection strategy

The task's third example build — "switch strategy based on positioning." The **macro** layer is a market-wide regime switch; the **market** layer's Fear & Greed reading sharpens it (extreme greed = overheated). (A derivatives funding/OI input would sharpen it further — not in this build; wire it in if you add a derivatives source.)

## Hypothesis
The same signal should be traded differently by regime. In a healthy/risk-on regime (easing macro, neutral/fearful sentiment) run **momentum** (follow the heat). In an overheated regime (extreme greed) switch to **mean-reversion / divergence-fade** (the crowd is the exit liquidity).

## Compose — it's two configs + a switch
1. **Read the regime**: `catalyst regime` for the macro side; `catalyst marketbias` for the Fear & Greed reading. Classify: `risk-on` vs `overheated`.
2. **Pick the mode:**
   - `risk-on` → run the **momentum** config (`examples/momentum.md`, high `--market-weight`).
   - `overheated` → run the **divergence** config (`examples/divergence.md`, high `--flow/--supply-weight`, fade the crowd).
3. **Backtest each mode over the matching sub-periods**, then report the switched strategy vs always-on momentum and vs BTC.

```bash
# momentum leg, over risk-on windows
catalyst backtest --from <riskon_from> --to <riskon_to> --weights momentum.json --market-weight 0.5 --db oracle.db
# divergence leg, over overheated windows
catalyst backtest --from <hot_from> --to <hot_to> --weights divergence.json --flow-weight 0.4 --supply-weight 0.4 --db oracle.db
```

## Spec
```yaml
name: regime-switch-momentum-vs-fade
hypothesis: "Momentum in risk-on regimes; fade the crowd when funding/greed are overheated."
universe: [BTC, ETH, SOL]
regime_rule:
  risk_on:   "macro risk-on AND F&G < 75            -> momentum config"
  overheated:"F&G >= 80 (extreme greed)             -> divergence config"
inputs:
  macro: catalyst regime
  sentiment: catalyst marketbias (Fear & Greed)
configs: { risk_on: momentum.json, overheated: divergence.json }
backtest: { from: 2026-01-01, to: 2026-06-01, base_size: 0.2, cost_bps: 10 }
results: { per_regime: {...}, switched_total: {...}, baseline_btc: {...} }
verdict: <keep if the switched strategy beats always-momentum and BTC on Sharpe/drawdown>
```

## Read the result
The win condition is specifically that **switching beats either single mode run alone** — otherwise the regime detection adds complexity without edge. Report all three (momentum-only, divergence-only, switched) side by side.
