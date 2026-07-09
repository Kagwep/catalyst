# catalyst

**catalyst** is a crypto **catalyst-signal oracle**. It ingests high-velocity
news and social posts (Bluesky, RSS/Atom, GitHub releases) plus numeric market
feeds (macro rates, ETF flows, on-chain supply, derivatives positioning, price
technicals), has an LLM read every post (sentiment, catalyst type, a one-line
"what happened", severity), and folds the lot into **per-asset watch-signals**
through a deterministic scoring engine — severity-weighted, story-deduped,
per-catalyst decay, confidence modified by five bias layers.

It sells those results as two paid services on the **[Croo Network](https://docs.croo.network/)**.

> **Proposals only — not financial advice.** The oracle surfaces
> catalyst-driven **watch-signals**. It does **not** size, place, or manage
> trades. There is no buy/sell/hold verb in any delivery: a directional call is
> a `signal` (`alert` | `watch`) plus a market `direction` (`bullish` |
> `bearish` | `neutral`). See the [Disclaimer](#disclaimer).

Public repo: [github.com/Kagwep/catalyst](https://github.com/Kagwep/catalyst) ·
Python 3.12+.

---

## For agents & buyers — catalyst on the Croo Network

catalyst runs as a **provider agent on the Croo Network**, a decentralized
agent-to-agent service marketplace. Orders settle **on-chain on Base mainnet in
USDC**, gas is **sponsored by the platform**, the delivery content hash is
written on-chain, and escrow **auto-settles to the provider on delivery**. You
don't need to run anything in this repo to buy a signal — you place an order
through Croo.

### Order flow (buyer's view)

```
place order on the marketplace
   └─ provider auto-accepts   ← gated on pipeline health + asset coverage
        └─ pay (USDC on Base, gas sponsored)
             └─ pipeline runs
                  └─ structured JSON delivered in-band   ← read it with get_delivery
                       └─ escrow auto-settles, order completes
```

Delivery is **in-band** over Croo (`deliver_order` → the buyer reads the JSON
with `get_delivery`) and normally lands well inside the service SLA — the paid
path only reads the already-ingested store and runs deterministic math, it does
no network ingestion. Orders with unparseable requirements, uncovered assets, or
an unhealthy/stale pipeline are rejected at the gate before any charge.

### The two services

| Service | Schema | Listing id (Croo) | What it is |
|---|---|---|---|
| **catalyst signals** | `catalyst.signals` v2.0 | `784122a0-b943-4b95-8d66-f7c7896e7eba` | The depth product — "the call" |
| **catalyst events** | `catalyst.events` v2.0 | `6669e91c-c617-4fea-938a-8b6bcc30038e` | The breadth product — "the radar" |

Full field-by-field schemas (and the Croo Dashboard registration notes) live in
**[DASHBOARD-SCHEMA.md](DASHBOARD-SCHEMA.md)** — the single source of truth.
Summaries below.

#### catalyst signals — the depth product ("the call")

ONE top-ranked signal for the requested asset(s): direction, confidence, score,
horizon, the catalysts that drove it, and a **per-layer breakdown** (macro /
flow / supply / derivs / trend) of exactly which modifiers pushed confidence
which way. When narration is on (provider has an Anthropic key), it adds
**grounded** LLM prose that only restates the computed numbers — never invents
them: `summary`, `catalyst_notes` (what each catalyst tag means / what actually
happened), and `layer_notes` (how each layer pushed).

**Requirement fields** (all optional, all plain strings — the Croo v2 form can't
register typed numbers or arrays, so multi-values are comma-separated):

| Field | Meaning |
|---|---|
| `assets` | Comma-separated tickers, e.g. `ARB,LDO`. Blank = full covered universe |
| `signal` | `alert` or `watch` |
| `direction` | `bullish` / `bearish` / `neutral` |
| `horizon` | Output horizon, e.g. `intraday` / `short` |
| `min_confidence` | Lowest confidence to return, e.g. `0.5` |
| `window` | Input lookback: `6h` / `48` (bare = hours) / `3d` / `1w`. Range 1h–1w, clamped. Blank = `24h` |

`window` sets how much **input history** feeds the signal; `horizon` filters the
**output** signal's time-horizon. Different axes — don't confuse them.

<details>
<summary>Sample <code>catalyst.signals</code> delivery (trimmed)</summary>

```json
{
  "schema": "catalyst.signals",
  "version": "2.0",
  "generated_at": "2026-07-08T14:03:11+00:00",
  "disclaimer": "Proposals only — not financial advice. The oracle proposes catalyst-driven signals; it does not size, place, or manage trades.",
  "count": 1,
  "actions": {
    "asset": "ARB",
    "signal": "alert",
    "direction": "bullish",
    "confidence": 0.71,
    "score": 0.34,
    "horizon": "intraday",
    "freshness": 12.0,
    "rationale": "BULLISH ALERT ARB | score +0.34 | sentiment +0.52 | strength 0.66 | 7 mention(s) | catalysts: upgrade,listing | macro risk-on (+0.40) | flow inflow (+0.55) | derivs crowded-long (-0.30)",
    "created_at": "2026-07-08T14:03:00+00:00"
  },
  "catalysts": ["upgrade", "listing"],
  "layers": {
    "macro":  { "label": "risk-on",      "bias":  0.40, "effect": "boost", "weight": 0.30 },
    "flow":   { "label": "inflow",       "bias":  0.55, "effect": "boost", "weight": 0.25 },
    "derivs": { "label": "crowded-long", "bias": -0.30, "effect": "damp",  "weight": 0.25 }
  },
  "summary": "ARB is a moderate-conviction bullish alert — an upgrade plus a listing across 7 mentions, with ETF-style inflows and a risk-on macro regime, lightly faded by crowded longs.",
  "catalyst_notes": { "upgrade": "protocol upgrade / release", "listing": "new exchange listing" },
  "layer_notes": { "flow": "ETF inflows nudged bullish", "macro": "risk-on regime", "derivs": "crowded longs faded the call" },
  "universe": ["ARB"],
  "requirements": { "assets": "ARB", "horizon": "intraday", "min_confidence": "0.5" }
}
```

When nothing is actionable for the requested asset, the delivery is a well-formed
neutral `watch` (never empty — an empty payload would fail the schema and expire
the order).
</details>

#### catalyst events — the breadth product ("the radar")

A market-wide feed of fresh, market-moving catalyst events, **one line each**:
`ASSET | catalyst | what happened | direction | severity | age`, plus a
structured `lead` object for the single most market-moving event. No LLM at
serve time — it reads the `event`/`severity` fields written at enrich time.
Market-wide macro events are labeled `MARKET`.

**Requirement fields** (all optional strings):

| Field | Meaning |
|---|---|
| `assets` | Comma-separated tickers, e.g. `BTC,ETH`. Blank = all |
| `catalysts` | Comma-separated types, e.g. `etf,hack,regulation`. Blank = all |
| `min_severity` | `high` / `medium` / `low`. Blank = `medium` (market-movers only) |
| `direction` | `bullish` / `bearish` / `neutral`. Blank = all |
| `window` | Lookback: `6h` / `3d` / `1w` (bare = hours), 1h–1w. Blank = `24h` |
| `limit` | Max events, e.g. `15`. Blank = `20` |

<details>
<summary>Sample <code>catalyst.events</code> delivery (trimmed)</summary>

```json
{
  "schema": "catalyst.events",
  "version": "2.0",
  "generated_at": "2026-07-08T14:03:11+00:00",
  "disclaimer": "Proposals only — not financial advice. The oracle proposes catalyst-driven signals; it does not size, place, or manage trades.",
  "count": 3,
  "events": [
    "ARB | upgrade | Arbitrum Nitro upgrade shipped | bullish | high | 12m ago",
    "MARKET | macro | Fed minutes read dovish on cuts | bullish | medium | 40m ago",
    "LDO | unlock | 1.2% of float unlocks in 3 days | bearish | medium | 2h ago"
  ],
  "lead": {
    "asset": "ARB",
    "catalyst": "upgrade",
    "event": "Arbitrum Nitro upgrade shipped",
    "direction": "bullish",
    "severity": "high",
    "sentiment": 0.52,
    "source": "github",
    "url": "https://github.com/OffchainLabs/nitro/releases/tag/v3.0.0",
    "at": "2026-07-08T13:51:00+00:00"
  },
  "assets": ["ARB", "LDO"],
  "catalysts": ["upgrade", "macro", "unlock"],
  "window_hours": 24,
  "requirements": { "min_severity": "medium" }
}
```
</details>

---

## What's under the hood

Everything rests on one idea: **every source becomes the same normalized record**,
distinguished only by its `source` field. That keeps each layer source-agnostic,
so adding a data source never disturbs the layers above it.

```
SOURCES                     ENRICH (per post)           SIGNAL LAYER
Bluesky / RSS / GitHub  ─►  Claude reads each post:  ─► per-asset aggregation:
DefiLlama / Snapshot        · sentiment                 · story dedup (N outlets = 1 story)
macro / flows / unlocks     · catalyst type             · severity weighting
staking / market / derivs   · one-line "what happened"  · per-catalyst time-decay
on-chain events             · severity                  · score = sentiment × strength

        │
        ▼
BIAS LAYERS (per-asset / market-wide confidence modifiers)
  macro regime · ETF flows · on-chain supply · derivatives positioning · multi-day trend
        │
        ▼
PLANNER  ── thresholds · staleness/cooldown gates · conflict detection · confidence calibration
        │
        ▼
DELIVERABLES
  catalyst.signals (depth)   catalyst.events (breadth)
  alert sinks · monitors     Croo deliver_order
```

- **Enrichment is hybrid.** A zero-dependency lexicon scores every post (keyless,
  offline); an optional **Claude** pass re-scores only the *candidates* (a
  catalyst, strong sentiment, or a primary high-signal account) to keep LLM cost
  low on a high-velocity feed.
- **The scoring engine is deterministic.** Recency decay, source/catalyst/severity
  weights, story dedup, and the `score = sentiment × strength` conviction all
  live in `catalyst/signals.py`; every weight is in `weights.json`.
- **Five bias layers** modify planner confidence: macro risk-regime (market-wide),
  BTC/ETH ETF flows, on-chain supply (token unlocks + ETH staking queue),
  derivatives positioning (crowded longs/shorts fade the aligned trade), and a
  multi-day trend slope. Each snapshots what it saw each cycle so the backtest
  can replay it point-in-time.

### Tuning & backtesting

The weights aren't hand-picked. `catalyst tune` random-searches the scorer's
weights over the real backtest and emits a self-describing **`weights.tuned.json`**
(fitted params + measured metrics); `catalyst calibrate` does a coordinate-ascent
sweep over the bias-layer weights and writes the winners back into `weights.json`.

The **backtest harness** (`catalyst backtest`) replays the real planner over
history with a strict point-in-time cut and scores each proposed call against
what prices actually did over its horizon. It reports two phases: **signal
quality** (hit-rate, per-horizon/catalyst/confidence breakdowns, a reliability
curve + `calibration_error`, a buy-and-hold-BTC baseline) and a **portfolio sim**
(confidence-sized positions, fees + slippage → total return, Sharpe, max
drawdown, win-rate, profit factor).

---

## Usage — developers

### Install

```bash
uv sync                     # core (includes the Croo SDK) + venv
uv sync --extra llm         # + anthropic, for the optional Claude enrich & narration
uv sync --extra pg          # + Postgres driver, for the hosted Supabase backend
uv sync --extra ml          # + pandas/pyarrow, for DataFrame/Parquet export
uv sync --extra dev         # + pytest/respx for the test suite
# or, without uv:
pip install -e ".[dev]"
```

This exposes a `catalyst` console command (run via `uv run catalyst …` or inside
the activated venv).

### Environment variables

All optional — everything runs keyless and offline by default.

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Enables the Claude enrichment pass (`--llm`) and the provider's grounded narration |
| `DATABASE_URL` / `DATABASE_PASSWORD` | Switch the store from local SQLite to hosted Postgres (Supabase). See [HOSTING.md](HOSTING.md) |
| `CROO_API_URL` / `CROO_WS_URL` / `CROO_SDK_KEY` | Run as a Croo provider. See [DEPLOY-PROVIDER.md](DEPLOY-PROVIDER.md) |
| `CROO_EVENTS_SERVICE_ID` | Set to also serve the second (`catalyst.events`) service off the same provider |
| `BLUESKY_HANDLE` / `BLUESKY_APP_PASSWORD` | Authenticated Bluesky search (the public AppView 403s datacenter IPs; auth isn't IP-blocked) |
| `FRED_API_KEY` | Optional numeric macro series (`macro --fred`) |
| `DERIVS_PROVIDER` | Force one perp provider: `binance` \| `bybit` \| `kraken` \| `hyperliquid` |

### Core CLI

Flags are verified against `catalyst/cli.py`. Every subcommand takes the shared
`--db PATH` (default `catalyst.db`), `--save`, and `--quiet`.

```bash
# The live oracle: fetch → enrich → signal → plan → alert, every 5 minutes
catalyst poll
catalyst poll --once                              # single cycle (for cron/Actions)
catalyst poll --llm-all --model claude-haiku-4-5  # LLM-score every post this cycle
```
`poll` runs the whole pipeline each cycle and dispatches alerts + records cycle
health. `--llm` scores only candidates; `--llm-all` bypasses the candidate gate
and scores every post (implies `--llm`). Bias layers are on by default
(`--no-macro`/`--no-flows`/`--no-supply`/`--no-market`/`--no-derivs` to drop one).

```bash
# Score stored posts (sentiment / asset / catalyst / severity)
catalyst enrich --llm --model claude-haiku-4-5 --primary watcher.guru
catalyst enrich --reenrich                        # re-score everything
```

```bash
# Rank per-asset signals from enriched posts (read-only analytics)
catalyst signals --window 48 --halflife 6 --asset BTC
```

```bash
# Propose ranked candidate actions (proposals only) from the signals
catalyst plan --buy-threshold 0.25 --max-age 120 --save
```

```bash
# Backtest the planner over history; tune the weights against it
catalyst backtest --from 2026-01-01 --to 2026-06-01 --trades
catalyst tune --window 30 --trials 25 --out weights.tuned.json
catalyst calibrate --metric sharpe --write weights.json
```

```bash
# Named catalyst watches (parallel to the global alert rule)
catalyst monitor add aave-treasury --catalysts treasury --assets AAVE \
    --on event --webhook https://hooks.example/telegram
catalyst monitor list
catalyst monitor check                            # dry-run the event path (preview)
```

```bash
# Operator health: last cycle, source freshness, open proposals, ops issues
catalyst status
```

```bash
# Run as a Croo provider agent (needs the Croo SDK + CROO_* env)
catalyst croo-provider --assets BTC,ETH           # restrict coverage; omit to cover all
catalyst croo-provider --no-op                    # SDK smoke test: accept + deliver a static probe
catalyst croo-provider --present-model claude-haiku-4-5   # cheaper narration model
```
`--no-op` proves auth + WS loop + accept/deliver against the real backend without
running the pipeline. Narration is on automatically when `ANTHROPIC_API_KEY` is
set; `--no-present` forces it off.

```bash
# Export the store to Parquet/CSV (needs the [ml] extra)
catalyst export --out posts.parquet
catalyst export --out posts.csv --format csv
```

There are more source-specific fetch commands (`search`, `author`, `rss`,
`follow`, `governance`, `protocols`, `macro`, `flows`, `unlocks`, `fng`,
`derivs`, `defillama`, `run`, `query`) and per-layer bias inspectors
(`regime`, `flowbias`, `supplybias`, `marketbias`, `derivsbias`) — run
`catalyst <cmd> -h` for flags.

### Tests

```bash
uv run pytest
```
The suite is **fully offline** — the Croo SDK is mocked and adapters are stubbed
(`respx`), so nothing hits the network. One live-sandbox Croo test is skipped
without credentials.

---

## Hosting topology

Two pieces run in two different places against **one shared database**:

```
INGESTION POLLER                          PROVIDER AGENT
GitHub Actions (cron, ~15 min)            always-on box (Railway)
  catalyst poll --once                      catalyst croo-provider
  + Supabase pg_cron pinger (exact 15m)     holds the Croo WebSocket
        │                                          │
        └──────────►  Supabase Postgres  ◄─────────┘
                      (DATABASE_URL)
```

The poller ingests + enriches on a cron and can't hold a socket, so it runs on
GitHub Actions (with a Supabase `pg_cron` → Edge Function pinger for an exact
cadence, since GitHub's cron is best-effort). The provider holds a persistent
Croo WebSocket, so it needs an always-on host (Railway), and it's **read-only**
against the same DB. Step-by-step:

- **[HOSTING.md](HOSTING.md)** — the poller on Supabase + GitHub Actions.
- **[DEPLOY-PROVIDER.md](DEPLOY-PROVIDER.md)** — the provider agent on Railway.

Local dev needs none of this: with `DATABASE_URL` unset you're on local SQLite
(`catalyst.db`).

---

## Disclaimer

catalyst is a research / signal-intelligence tool. It produces **proposed
watch-signals with rationale** — nothing more.

- **Proposals only.** The oracle surfaces catalyst-driven signals; it never
  sizes, places, or manages trades.
- **Watch-signal framing.** No delivery carries a buy/sell/hold instruction. A
  directional call is a `signal` (`alert` | `watch`) plus a market `direction`
  (`bullish` | `bearish` | `neutral`). The disclaimer is baked into every payload.
- **Not financial advice.** Position sizing, risk limits, and execution are the
  operator's responsibility. Nothing here is financial advice.
