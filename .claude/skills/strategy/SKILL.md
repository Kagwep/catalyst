---
name: strategy
description: Author a backtestable crypto trading-strategy spec from market data. Use when asked to design, generate, tune, or backtest a trading strategy (a momentum/divergence/regime strategy, or "turn this market view into a strategy"). Composes a catalyst config across the signal/macro/flows/supply/market layers, backtests it, and emits a reproducible strategy spec (config + measured performance). Proposals only — never live trades.
---

# Strategy Skill

Turn market data into a **backtestable strategy spec** — a weights/threshold config plus its measured backtest performance. This is strategy *generation* (Quantopian-style, adapted to crypto), **not** live trading: every output is a spec, never an order. Sizing, risk, and execution stay with the operator.

## The engine you drive

`Catalyst` scores per-asset signals from news/sentiment, layers context modifiers onto a planner, and backtests the result. You author a strategy by choosing **which layers to lean on, with what weights and thresholds**. The full knob + CLI catalog is in `reference.md` — read it before composing.

The layers (each a per-asset or market-wide confidence modifier on the planner):

| Layer | What it reads | Knobs |
|---|---|---|
| **signal** | news/social sentiment — the core alpha | `source_weights`, `catalyst_weights`, `buy_threshold` |
| **macro** | market-wide risk regime (rates/inflation) | `--macro-weight` |
| **flows** | BTC/ETH ETF demand (per-asset) | `--flow-weight`, `flow_scale` |
| **supply** | token unlocks (bearish) + ETH staking (bullish) | `--supply-weight`, `unlock_scale`, `stake_scale` |
| **market** | price momentum: RSI/MACD + Fear & Greed | `--market-weight`, `fng_weight`, `macd_scale` |

## Live read (current context)

Ground the strategy **before** composing, using the layer inspection commands (all free, no key):
- `catalyst marketbias` — price momentum (RSI/MACD) + Fear & Greed
- `catalyst regime` — market-wide risk regime (rates/inflation)
- `catalyst flowbias` / `catalyst supplybias` — ETF demand and unlock/staking supply

These read current state; the backtest replays stored history. Use the live read to pick the universe and sanity-check the hypothesis, then compose.

## The loop

1. **Hypothesis** — one sentence: what edge, in what regime. (Check it against the live read.)
2. **Compose** — pick layers + weights + thresholds → a `weights.json`-shaped config + plan flags.
3. **Backtest** — `catalyst backtest --from <date> --to <date> --weights <config>.json --trades`.
4. **Read** — hit-rate and breakdown (Phase 1, signal quality); total return, **Sharpe**, **max drawdown** (Phase 2, net-of-fees P&L). Always against the **BTC baseline**.
5. **Iterate** — tune weights; A/B two configs with `catalyst compare`. Keep what beats the baseline at an acceptable drawdown; discard the rest.
6. **Emit the spec** — the schema below, with the `results` filled from the backtest.

## Strategy-spec schema (the deliverable artifact)

```yaml
name: <slug>
hypothesis: <one sentence — the edge and when it holds>
universe: [BTC, ETH, ...]
config:
  weights: { ... }                 # weights.json overrides (the tuned knobs)
  thresholds: { buy, watch, cooldown, max_age }
  layers: { macro, flows, supply, market + their weights, what's off }
  horizons: { intraday: 24h, short: 72h }
backtest: { from, to, base_size, max_position, cost_bps }
results:                            # filled by running `catalyst backtest`
  signal_quality: { hit_rate, mean_return, by_catalyst }
  portfolio: { total_return, sharpe, max_drawdown, profit_factor }
  baseline_btc: <return over window>
verdict: <keep / discard + why, vs baseline and drawdown>
```

The spec is self-contained and reproducible: the `config` + `backtest` block re-runs the same numbers.

## The three reference strategies (the task's example builds)

- **Momentum** → `examples/momentum.md` — lean on the **market** layer (RSI/MACD/Fear & Greed).
- **Sentiment-divergence** → `examples/divergence.md` — fire when **signal** disagrees with **flows/supply** (crowd vs smart money).
- **Regime-detection** → `examples/regime.md` — let **macro** + the **market** layer (Fear & Greed) switch which mode runs.

Start from the closest example, then tune.

## Guardrails

- **Proposals only.** Never place, size, or manage live trades. The deliverable is a spec.
- **Always report the BTC baseline and the trade count.** Don't over-read a handful of trades.
- **Be honest about coverage.** Unmapped tickers are skipped; the macro/flows/supply layers are only as deep as the accumulated history (Phase-1 signal quality is broadest; bias-layer backtests improve as history builds).
- **Two numbers, two meanings.** Phase-1 hit-rate = does the signal predict moves; Phase-2 Sharpe/drawdown = net-of-fees portfolio P&L. Quote both.
