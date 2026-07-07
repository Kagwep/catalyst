# Catalyst knob & CLI reference

The toolset behind the Skill. Compose a strategy by setting these, then backtest.

## Pipeline (build history first)

```bash
catalyst poll --once --config sources.json --db oracle.db   # ingest + enrich + plan one cycle
catalyst poll --config sources.json --db oracle.db          # run continuously (accumulates history + bias snapshots)
```

Each cycle ingests every source, enriches, plans, and **snapshots every layer's bias** (`bias_snapshots`) so the bias layers become point-in-time replayable. The backtest is only as deep as what's been accumulated (plus the natively-dated sources: news, unlocks, flows, Fear & Greed, FRED).

## Inspect the layers (current reads)

```bash
catalyst regime    --db oracle.db                  # market-wide risk regime (macro)
catalyst flowbias  --db oracle.db                   # BTC/ETH ETF demand bias
catalyst supplybias --db oracle.db                  # unlocks + staking supply bias
catalyst marketbias --assets BTC,ETH --db oracle.db # RSI/MACD + Fear & Greed momentum
```

## Plan (propose actions with the modifiers)

```bash
catalyst plan --db oracle.db --buy-threshold 0.2 \
  --macro-weight 0.3 --flow-weight 0.25 --supply-weight 0.25 --market-weight 0.25
```
Toggle any layer off with `--no-macro` / `--no-flows` / `--no-supply` / `--no-market`. Each action carries a `rationale` that names which modifiers moved its confidence.

## Backtest (the measurement)

```bash
catalyst backtest --from 2026-01-01 --to 2026-06-01 --db oracle.db \
  --weights strategy.json --base-size 0.2 --cost-bps 10 --trades
```
- **Phase 1** (signal quality): hit-rate, mean/median/cumulative return, by horizon / catalyst / confidence, vs BTC baseline.
- **Phase 2** (`--portfolio`, on): total return, **Sharpe**, **max drawdown**, win-rate, profit factor, fees — confidence-sized, net of `--cost-bps`.
- `--intraday-hours 24 --short-hours 72` set the holding periods.

## Tune & compare

```bash
catalyst compare --a configA.json --b configB.json --db oracle.db   # A/B two configs
```
`weights.json` is the tuning surface. Keys that matter per layer:

| Key | Layer | Effect |
|---|---|---|
| `source_weights`, `catalyst_weights`, `primary_boost`, `strength_saturation` | signal | how much each source/catalyst counts |
| `flow_scale` (per asset) | flows | bigger = less sensitive to ETF flow |
| `unlock_scale`, `stake_scale`, `exit_weight`, `horizon_days` | supply | unlock/staking sensitivity |
| `fng_weight`, `macd_scale` | market | Fear & Greed nudge; MACD sensitivity |

Plan-time weights (`--macro-weight` etc., default 0.3/0.25) scale how hard each layer moves confidence — turning one up and the rest down is how you specialize a strategy (e.g. a momentum strategy = high `--market-weight`, low sentiment reliance).

## Data sources (all free unless noted)

- News/social: Bluesky + RSS; protocol releases (GitHub), governance (Snapshot), risk (DefiLlama)
- macro: central-bank RSS + FRED (`--history N` backfills dated observations)
- flows: Farside ETF flows (BTC/ETH)
- supply: DefiLlama unlocks + ETH beacon-node staking queue
- market: price history (DefiLlama coins, for RSI/MACD) + Fear & Greed (alternative.me, free)
- prices (backtest scoring): DefiLlama coins `/chart`
