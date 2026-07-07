# Example: momentum strategy

The task's first example build — "blend RSI, MACD, and Fear & Greed into entry/exit rules." In Catalyst this is the **market** layer turned up, with sentiment kept as a light confirmation.

## Hypothesis
Ride established price momentum: go with assets where RSI and MACD agree on direction and market-wide Fear & Greed isn't fighting them; stand down when momentum and crowd sentiment diverge.

## Compose
- **Lean on:** the market layer (`--market-weight 0.5`).
- **Light touch:** sentiment as confirmation, not driver (keep signal but raise `buy_threshold` so only strong reads fire).
- **Off / low:** flows and supply (momentum strategy doesn't need ETF/unlock context) — `--no-supply`, low `--flow-weight`.
- **Knobs** (`momentum.json`): higher `macd_scale` for snappier MACD, `fng_weight` ~0.3.

```bash
catalyst backtest --from 2026-01-01 --to 2026-06-01 --db oracle.db \
  --weights momentum.json --market-weight 0.5 --no-supply --buy-threshold 0.25 \
  --base-size 0.2 --cost-bps 10 --trades
```
Live read first: `catalyst marketbias --assets BTC,ETH,SOL` to confirm trend (RSI/MACD) and the Fear & Greed reading before setting the universe.

## Spec
```yaml
name: momentum-rsi-macd-fng
hypothesis: "Ride momentum when RSI+MACD agree and Fear&Greed isn't against it."
universe: [BTC, ETH, SOL]
config:
  weights: { macd_scale: 30, fng_weight: 0.3 }
  thresholds: { buy: 0.25, cooldown: 120 }
  layers: { market: 0.5, macro: 0.2, flows: 0.1, supply: off }
  horizons: { intraday: 24h, short: 72h }
backtest: { from: 2026-01-01, to: 2026-06-01, base_size: 0.2, cost_bps: 10 }
results: { signal_quality: {...}, portfolio: {...}, baseline_btc: {...} }
verdict: <keep if Sharpe and hit-rate beat BTC baseline at tolerable drawdown>
```

## Read the result
Momentum should shine in trending windows and bleed (fees + whipsaw) in chop — check `by_confidence` and the equity curve, and compare the trending vs ranging sub-periods.
