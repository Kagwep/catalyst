# Example: sentiment-divergence strategy

The task's second example build — "flag when social heat and on-chain flow disagree." This is **native** to Catalyst: the signal layer is social heat; the flows and supply layers are the money. When they disagree, the planner's modifiers already fade the crowd — this strategy leans into that.

## Hypothesis
When the crowd is loudly bullish (`signal`) but the money is leaving (ETF outflow / imminent unlock), fade the crowd; when the crowd is bearish but money is quietly accumulating (ETF inflow / staking lockup), lean contrarian-long. Agreement = high conviction.

## Compose
- **Lean on:** flows + supply as the contradiction axis (`--flow-weight 0.4 --supply-weight 0.4`).
- **Core:** keep the signal layer (it's the "social heat" side of the divergence).
- **Light:** market/macro as context.

```bash
catalyst backtest --from 2026-01-01 --to 2026-06-01 --db oracle.db \
  --weights divergence.json --flow-weight 0.4 --supply-weight 0.4 \
  --base-size 0.2 --cost-bps 10 --trades
```
The divergence is automatic: a buy signal with money flowing *out* gets damped (often below the action threshold), while agreement gets boosted — read the action `rationale` to see "flow distribution" / "supply pressure" notes firing.

## Spec
```yaml
name: sentiment-flow-divergence
hypothesis: "Fade social heat when ETF flow / unlocks disagree; high conviction when they agree."
universe: [BTC, ETH, ARB, OP]
config:
  weights: { flow_scale: { BTC: 1.0e9, ETH: 2.5e8 } }
  thresholds: { buy: 0.2, cooldown: 120 }
  layers: { flows: 0.4, supply: 0.4, market: 0.15, macro: 0.2 }
  horizons: { intraday: 24h, short: 72h }
backtest: { from: 2026-01-01, to: 2026-06-01, base_size: 0.2, cost_bps: 10 }
results: { signal_quality: {...}, portfolio: {...}, baseline_btc: {...} }
verdict: <keep if divergence-faded trades out-hit the naive sentiment-only run>
```

## Read the result
The key comparison: A/B this against a sentiment-only config (flows/supply off) via `catalyst compare`. The divergence edge is real only if fading improves hit-rate / Sharpe over naive sentiment.
