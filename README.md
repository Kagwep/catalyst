# Catalyst

**Catalyst is a catalyst-driven crypto signal oracle that runs as a hireable
provider agent on the [Croo Network](https://docs.croo.network/).** It ingests
high-velocity signals — breaking news, social posts, protocol activity, flows,
and on-chain events — scores them, and turns them into **ranked, proposed trade
actions** with a full rationale. Another agent (or a human) hires it over Croo's
agent-to-agent marketplace, pays per call in USDC on Base, and receives a
structured `Action[]` deliverable. The oracle **proposes**; it never sizes or
places trades.

Python 3.12+. Built with `httpx`, `feedparser`, `pydantic`, and the `croo-sdk`;
storage is the stdlib `sqlite3`. Every data source favors keyless public
endpoints — no API keys required to run the pipeline.

## The deliverable — what you get when you hire it

When a paid order arrives, Catalyst runs its pipeline and delivers **one
canonical JSON payload** (`catalyst.actions` schema): the planner's ranked
proposals, each self-describing enough to act on with no other context — what to
do, how sure, over what horizon, which catalysts drove it, and a machine-readable
`layers` breakdown of exactly which context modifiers (macro / flows / supply /
market / derivatives) pushed the confidence which way. The proposal disclaimer is
baked in.

The *same* payload is what an alert webhook emits and what a Croo `deliver_order`
sends — one shape, no drift between channels. A real sample:

```json
{
  "schema": "catalyst.actions",
  "version": "1.0",
  "generated_at": "2026-07-02T14:03:11+00:00",
  "disclaimer": "Proposals only — not financial advice. The oracle proposes catalyst-driven signals; it does not size, place, or manage trades.",
  "count": 2,
  "actions": [
    {
      "asset": "ARB",
      "action": "buy",
      "direction": "bullish",
      "confidence": 0.71,
      "horizon": "intraday",
      "score": 0.34,
      "catalysts": ["upgrade", "listing"],
      "freshness_minutes": 12.0,
      "layers": {
        "macro":  { "label": "risk-on",      "bias":  0.40, "effect": "boost", "weight": 0.30 },
        "flows":  { "label": "inflow",       "bias":  0.55, "effect": "boost", "weight": 0.25 },
        "supply": { "label": "neutral",      "bias":  0.02, "effect": "boost", "weight": 0.25 },
        "derivs": { "label": "crowded-long", "bias": -0.30, "effect": "damp",  "weight": 0.20 }
      },
      "rationale": "BUY ARB (bullish) | score +0.34 | sentiment +0.52 | strength 0.66 | 7 mention(s) | catalysts: upgrade,listing | velocity 3 | latest 12m ago | macro risk-on (boost) | flows inflow (boost) | derivs crowded-long (damp)",
      "created_at": "2026-07-02T14:03:00+00:00"
    },
    {
      "asset": "LDO",
      "action": "sell",
      "direction": "bearish",
      "confidence": 0.63,
      "horizon": "short",
      "score": -0.29,
      "catalysts": ["unlock"],
      "freshness_minutes": 40.0,
      "layers": {
        "supply": { "label": "unlock-pressure", "bias": -0.48, "effect": "boost", "weight": 0.25 },
        "flows":  { "label": "outflow",         "bias": -0.20, "effect": "boost", "weight": 0.25 }
      },
      "rationale": "SELL LDO (bearish) | score -0.29 | sentiment -0.38 | strength 0.61 | 5 mention(s) | catalysts: unlock | latest 40m ago | supply unlock-pressure (boost)",
      "created_at": "2026-07-02T14:03:00+00:00"
    }
  ],
  "meta": {
    "universe": ["ARB", "LDO"],
    "requirements": { "assets": ["ARB", "LDO"], "horizon": "intraday", "min_confidence": 0.5 }
  }
}
```

A buyer scopes the order with a small **requirements** object —
`{ "assets": [...], "horizon": "intraday|short", "min_confidence": N }` — and the
delivery is filtered to exactly what they asked for (`meta.requirements` echoes
it back; `meta.universe` is everything the pipeline covered this run).

## How it works — the pipeline

Everything rests on one idea: **every source — a tweet, a news headline, a
GitHub release, an on-chain event — becomes the same normalized record**,
distinguished only by its `source` field. That keeps each layer source-agnostic,
so adding a data source never disturbs the layers above it.

```
1. INGEST         ✅  Pull many sources → normalize → de-dupe → SQLite (on demand or on a timer)
2. ENRICH         ✅  Score each item: market sentiment + asset/ticker + catalyst type
3. SIGNAL         ✅  Aggregate per asset: weighted sentiment + volume + recency + catalyst → ranked score
4. PLANNER        ✅  Ranked candidate actions {asset, action, confidence, horizon, why} + context modifiers
5. BACKTEST       ✅  Replay the planner over history → score proposals on prices → portfolio P&L (sizing, fees, Sharpe)
6. ALERTS         ✅  Rule-gated, de-duplicated delivery of the canonical payload to pluggable sinks
7. MONITORING     ✅  Per-cycle health record → ops alerts (silent source, error streak, slow cycle, LLM budget)
8. CROO PROVIDER  ✅  Run as a marketplace agent: accept paid orders, run the pipeline, deliver the Action[]
9. EXECUTION      ◻︎  Gated / out of scope: the oracle proposes; sizing, risk, and execution stay with the operator
```

### Data sources

| Domain | Source | Status |
|---|---|---|
| **News / social** | Bluesky (AT Protocol) — keyword search + author feeds | ✅ |
| **News** | RSS / Atom feeds (any publisher) | ✅ |
| **Curated accounts** | A plaintext handle file (`watcher.guru` as the fast primary, wires for depth) | ✅ |
| **Protocol releases** | GitHub release/tag/commit Atom feeds (via the RSS adapter) | ✅ |
| **Protocol risk/ecosystem** | DefiLlama (hacks, TVL moves, new listings) | ✅ |
| **Governance** | Snapshot DAO proposals & votes (GraphQL) | ✅ |
| **Macro (rates/inflation)** | Central-bank press (Fed/ECB RSS, no key) + optional FRED series | ✅ |
| **Flows (BTC/ETH)** | Spot-ETF net in/out (Farside, no key) → per-asset directional bias | ✅ |
| **Token unlocks** | Scheduled vesting cliffs (DefiLlama emissions, no key) → per-asset supply pressure + standalone catalyst | ✅ |
| **Staking queue** | ETH validator entry queue (beacon node, direct, no key) → per-asset supply sink | ✅ |
| **Market / momentum** | Price technicals RSI/MACD (DefiLlama prices) + Fear & Greed (alternative.me, no key) → per-asset momentum bias | ✅ |
| **On-chain actions** | Proxy upgrades, timelocked governance, treasury moves — from Ethereum event logs (public JSON-RPC, no key) → per-asset upgrade/timelock/treasury catalysts | ✅ |
| **Derivatives** | Perp funding + open interest (Binance → Bybit fallback, no key) → per-asset positioning bias (crowded longs/shorts fade the aligned trade) | ✅ |

A **`protocols.json` registry** ties each protocol to its GitHub repos, Snapshot
space, DeFiLlama slug, and **token symbol** — so a release or governance proposal
attributes to a tradeable asset (an Arbitrum Nitro release → `$ARB`, a Lido
proposal → `$LDO`) and flows through the same enrich → signal → plan pipeline.

The **primary** news source is `watcher.guru` (fast breaking + crypto/markets);
the wires (Reuters, NYT, CNN) and RSS feeds add confirmation depth. Protocol
releases already flow in through the RSS adapter (a GitHub `releases.atom` URL is
just another feed).

### Enrichment & sentiment

A **hybrid** scorer keeps cost low on a high-velocity feed: a zero-dependency
lexicon scores every item (directional sentiment, `$ticker`/coin extraction,
catalyst type), and an optional **Claude** pass re-scores only the *candidates* —
items with a catalyst, strong sentiment, or from a primary high-signal account.
Scores are written back as derived columns and flow into queries and exports.

## Selling the signal — the Croo provider

Catalyst runs as a **provider agent on the [Croo Network](https://docs.croo.network/)**
— a decentralized agent-to-agent marketplace with on-chain escrow settlement on
Base in USDC. Croo is the storefront + checkout + delivery rail; the pipeline is
the product. `catalyst croo-provider` stands up an async event loop
(`catalyst/croo_agent.py`) over the Croo SDK:

```
NEGOTIATION_CREATED ─► gate (parseable requirements + covered assets + healthy pipeline) ─► accept / reject
ORDER_PAID          ─► run the pipeline off the event loop ─► deliver_order(SCHEMA = the Action[] payload)
ORDER_COMPLETED     ─► delivery verified, USDC escrow settles to our AA wallet
```

- **Gated accept.** `accept_negotiation` mints an on-chain order, so it's gated:
  reject when requirements are unparseable, the requested assets aren't covered,
  or the Phase-7 monitoring surface says the pipeline is unhealthy/stale.
- **Idempotent delivery.** The paid handler guards on a local delivered-set *and*
  the on-chain order status — a WebSocket reconnect can redeliver an event, and it
  must never double-run the pipeline or double-deliver.
- **Never blocks the socket.** The sync pipeline runs in `asyncio.to_thread` so
  the WS heartbeat keeps ticking.
- **Same payload as everywhere else.** The deliverable is `payload.build_payload`
  — a Croo delivery is literally "just another sink."

The Croo SDK is **runtime-only**: creating the agent, registering the service,
issuing the SDK-Key, and funding the agent's AA wallet with USDC are all
Dashboard-side. Register the service with `requirements_type=schema`,
`deliverable_type=schema`, **Require Fund Transfer OFF** (that model is Phase-9
execution only), and an `sla_hours` above worst-case pipeline run time (read a
real cycle duration from `catalyst status`). Then set `CROO_API_URL`,
`CROO_WS_URL`, `CROO_SDK_KEY` (and optional `BASE_RPC_URL`) and run:

```bash
catalyst croo-provider --assets BTC,ETH        # restrict coverage; omit to cover all
```

## Project status

**All of the build plan's phases 1–8 are complete** (Phase 9 execution is gated
and out of scope by default). The full chain is built and tested: ingestion
(Bluesky + RSS + curated handles + GitHub release feeds + DefiLlama +
Snapshot governance + macro + flows + unlocks/staking + market + on-chain events
+ derivatives), SQLite persistence with upsert/dedup, a polling scheduler, the
hybrid enrichment layer, the **signal** layer, the **planner** with calibrated
confidence and staleness/cooldown/conflict gates, a two-phase **backtest**
harness, the **alerts** delivery layer, the **monitoring** health layer, and the
**Croo provider** runtime. The suite is **156 tests, fully offline** (plus one
live-sandbox test skipped without credentials) — the SDK is mocked, so nothing
hits the network in unit tests.

Around the core signal sit context layers, each a per-asset (or market-wide)
modifier on the planner: a **`protocols.json` registry** (GitHub releases +
Snapshot governance, attributed to token symbols); a **macro layer** (central-bank
press → a market-wide risk-regime modifier); a **flows layer** (BTC/ETH spot-ETF
in/out → demand bias); an **on-chain tier** (the supply mirror of flows: token
unlocks → bearish supply pressure, ETH staking queue → bullish supply sink); a
**market layer** (price momentum — RSI/MACD nudged by Fear & Greed); and a
**derivatives layer** (perp funding + OI → crowded-positioning fade). Every weight
lives in `weights.json`, tuned against the backtest (not by hand) via `catalyst
calibrate`, with `compare` for A/B.

Each layer also **snapshots what it saw each cycle** (`bias_snapshots`) so its
bias is replayable point-in-time — the basis for honest backtesting.

### Two ways to consume Catalyst

- **The live signal (the Croo product).** Hire the provider agent; get the
  fresh `Action[]` deliverable per paid order. This is what the platform tailoring
  is for.
- **The `strategy` Skill (the offline deliverable).** Authored at
  `.claude/skills/strategy/`, it turns market data into a *backtestable strategy
  spec* — a weights/threshold config plus its measured performance. Read current
  context via the layer inspection commands, compose a config across the layers,
  `catalyst backtest` it, iterate with `catalyst compare`, emit the spec. It ships
  worked builds for **momentum**, **sentiment-divergence**, and
  **regime-detection**.

> ⚠️ This is a research/engineering tool. It produces **proposed** signals with
> rationale — it does not place trades. Position sizing, risk limits, and
> execution are the operator's responsibility. Nothing here is financial advice.

## Install

```bash
uv sync                       # core (includes croo-sdk) + creates the venv
uv sync --extra dev           # + pytest/respx for tests
uv sync --extra ml            # + pandas/pyarrow for DataFrame access
uv sync --extra llm           # + anthropic for the optional Claude enrich pass
# or, without uv:
pip install -e ".[dev]"
```

This exposes a `catalyst` console command (run via `uv run catalyst …` or inside
the activated venv).

## Usage

```bash
# Search all of Bluesky by keyword
catalyst search "climate policy" --max 50 --sort latest

# An account's posts + reposts (handle or DID)
catalyst author nytimes.com --max 50

# Bluesky works keyless from residential IPs, but the public AppView 403s most
# datacenter/cloud IPs. On a hosted box set BLUESKY_HANDLE + BLUESKY_APP_PASSWORD
# (Settings → Privacy and Security → App Passwords) and the adapter switches to
# authenticated XRPC via the PDS, which is not IP-blocked.

# An RSS or Atom feed (same normalized output as Bluesky)
catalyst rss https://feeds.bbci.co.uk/news/rss.xml --max 25

# Monitor a protocol's releases — GitHub Atom feeds are just RSS
catalyst rss https://github.com/ethereum/go-ethereum/releases.atom --max 5

# Protocol signals from DefiLlama: hacks, TVL moves, new listings
catalyst defillama --only hacks --hack-days 90 --min-hack 5000000
catalyst defillama --only tvl --min-change 20 --min-tvl 50000000

# Snapshot DAO governance proposals
catalyst governance --spaces uniswapgovernance.eth,aave.eth --state active

# Registry-driven: GitHub releases + governance for every protocol in protocols.json
catalyst protocols --file protocols.json

# Batch-fetch everything in sources.json, de-duped & newest-first
catalyst run --config sources.json
```

Fetch commands print a JSON array to stdout (pipe it, or `--quiet` to suppress):

```bash
catalyst search "election" --max 100 > out.json
```

## Persistence (SQLite)

Add `--save` to any fetch command to upsert into SQLite (`run`/`poll` save by
default). Default DB path is `catalyst.db`; override with `--db PATH`.

```bash
catalyst search "election" --save --quiet      # DB only, no stdout
catalyst run --db news.db                       # batch -> DB
catalyst query --limit 20                        # read back, newest-first
catalyst query --source rss --limit 50           # filter by source
```

- Posts are keyed by their URI, so re-running never creates duplicates.
- Re-fetching a known post **updates its engagement metrics** while preserving
  the original `fetched_at`.

## Sentiment enrichment

Score stored posts for **sentiment + asset + catalyst** as a derived layer
(the adapters stay source-agnostic). Hybrid by design:

1. A zero-dependency **lexicon** scores every post (directional market
   sentiment, `$ticker`/coin extraction, catalyst type: listing/hack/etf/
   mainnet/regulation/partnership/liquidation/unlock/upgrade/timelock/treasury).
2. An optional **Claude** pass re-scores only the *candidates* — posts with a
   catalyst, strong sentiment, or from a primary high-signal account
   (e.g. `watcher.guru`) — keeping LLM cost low on a high-velocity feed.

```bash
catalyst enrich --db oracle.db                         # lexicon only (free, offline)
catalyst enrich --db oracle.db --llm --model claude-haiku-4-5 --primary watcher.guru
catalyst enrich --db oracle.db --reenrich              # re-score everything
```

The LLM pass needs the `[llm]` extra (`pip install 'catalyst[llm]'`) and
`ANTHROPIC_API_KEY`. It defaults to `claude-opus-4-8`; for a fast, low-cost pass
on the firehose use `--model claude-haiku-4-5`.

Results are written to nullable columns (`sentiment_score`, `sentiment_label`,
`assets`, `catalyst`, `sentiment_model`, `enriched_at`) and flow into `query`
and the Parquet/DataFrame export automatically. Re-running only scores
not-yet-enriched posts unless you pass `--reenrich`.

## Signals

Aggregate the enriched posts into **ranked per-asset trade signals**. Each
asset's mentions are weighted by recency (exponential time-decay), source
(primary handles + DefiLlama boosted), catalyst type (hack/etf/listing amplify),
and engagement, then combined into a signed conviction `score = sentiment ×
strength`, plus a mention-`velocity` term. Read-only analytics over the store.

```bash
catalyst signals --db oracle.db                          # top signals, last 24h
catalyst signals --db oracle.db --window 48 --halflife 6 --min-strength 0.1
catalyst signals --db oracle.db --asset BTC              # one ticker
```

Each signal reports `asset`, `direction` (bullish/bearish/neutral), `score`,
`sentiment`, `strength`, `mentions`, `velocity`, the `catalysts` and `sources`
seen, and a few sample texts. Sharper, more decisive signals come from running
`enrich --llm` first.

## Macro regime (rates & inflation)

Rates/inflation news is **market-wide**, not per-asset — so the macro layer
ingests central-bank press (Fed/ECB RSS, no key) as `source="macro"` posts,
enriches them, and aggregates them into a single **risk regime** score
(risk-on / neutral / risk-off). Easing/cuts/cooling = risk-on; hikes/tightening
= risk-off.

```bash
catalyst macro                          # Fed/ECB press as macro posts (add --fred for numbers)
catalyst macro --save --db oracle.db    # persist them for the regime
catalyst regime --db oracle.db          # compute the current risk-on/off score
```

The **planner consumes the regime** (`plan`/`poll`, on by default): it scales
each trade's confidence by `--macro-weight` — boosting buys in a risk-on regime
and sells in risk-off, damping the opposite — and notes it in the rationale.
Disable with `--no-macro`. The optional **FRED** path (`--fred`, needs
`FRED_API_KEY`) pulls actual rate/CPI numbers with directional wording.

## Flows (BTC/ETH ETF in/out)

Where the macro regime is **market-wide**, flows are **per-asset**: how much real
money is moving into or out of the BTC and ETH spot ETFs. The flows layer scrapes
daily net flows from Farside (no key) as `source="flows"` posts and turns them
into a continuous bias per asset — net **inflow** = accumulation (bullish),
net **outflow** = distribution (bearish). The bias is `tanh(decay-weighted
net_usd / scale)`; the per-asset `scale` lives in `weights.json` (`flow_scale`).

```bash
catalyst flows --save --db oracle.db            # scrape BTC/ETH ETF flows, persist
catalyst flows --assets BTC --max-days 10       # just BTC, last 10 days
catalyst flowbias --db oracle.db                # current per-asset bias (inspect)
```

The **planner consumes the bias** (`plan`/`poll`, on by default): it scales each
trade's confidence by `--flow-weight`, per asset — a buy with money flowing *in*
is boosted, a buy while money flows *out* is damped. That last case is the
**sentiment/flow divergence fade**. Disable with `--no-flows`.

## On-chain tier (unlocks & staking)

The **supply-side mirror** of flows: where flows measure demand (money in/out),
the on-chain tier measures supply about to become sellable. Two sources, merged
into one per-asset **supply bias** (−1 supply pressure … +1 supply sink) that
modifies planner confidence like flows.

- **Token unlocks** (DefiLlama emissions, no key) — scheduled vesting **cliffs**
  for the tokens in `protocols.json`, filtered to real sell-pressure categories.
  Pressure ramps up as the unlock nears (normalized by % of float). Unlocks also
  ride the enrich → signal path as a **standalone `unlock` catalyst**.
- **ETH staking queue** (beacon node, read directly) — the validator entry queue
  is ETH being locked up = supply leaving the float (bullish). ETH-only.

```bash
catalyst unlocks --save --db oracle.db          # upcoming unlocks + ETH staking queue
catalyst supplybias --db oracle.db              # current per-asset supply bias (inspect)
```

Scale it with `--supply-weight`; disable with `--no-supply`. Scales live in
`weights.json` (`unlock_scale`, `stake_scale`, `exit_weight`, `horizon_days`).

> Note: unlock schedules get revised and the beacon node only serves current
> state, so there's no free point-in-time history — the tier becomes honestly
> *backtestable* only once it's been snapshotted forward for a while.

## Market layer (price momentum)

Price technicals the news/supply layers don't capture: **RSI + MACD** computed
from free DefiLlama price history, nudged by the market-wide **Fear & Greed**
index (free from alternative.me). Output is a per-asset momentum bias
(−1 bearish .. +1 bullish) — the basis for the momentum strategy.

```bash
catalyst fng --save --db oracle.db                  # ingest the Fear & Greed series
catalyst marketbias --assets BTC,ETH --db oracle.db  # current RSI/MACD + F&G bias
```

Scale with `--market-weight`, disable with `--no-market`; `fng_weight` /
`macd_scale` live in `weights.json`. Because technicals come from real price
history, this layer is **backtestable today** (no snapshotting needed).

## Derivatives layer (funding & open interest)

Keyless perp **funding + open interest** → a per-asset **positioning
bias**: crowded longs fade bullishness, crowded shorts fade bearishness (the
aligned trade is the crowded one, so it's the one to be wary of). Data comes
from a provider chain — Binance first, Bybit v5 fallback (Binance 451s US
datacenter IPs, e.g. GitHub Actions runners); force one with
`DERIVS_PROVIDER=binance|bybit`.

```bash
catalyst derivs --save --db oracle.db           # funding + OI per asset, persist
catalyst derivsbias --db oracle.db              # current positioning bias (inspect)
```

Scale with `--derivs-weight`; `funding_scale` lives in `weights.json`. Text uses
the exchange symbol so it never leaks into the news-signal layer.

## Plan (candidate actions)

Turn ranked signals into prioritized candidate **actions**. It applies a
conviction threshold, a **staleness gate** (a reactionary signal older than
`--max-age` downgrades to `watch`; intraday/fast catalysts expire sooner via
`--fast-max-age`), a **cooldown** (same asset+action won't re-fire within
`--cooldown` minutes — but a materially more-confident repeat can break it), and
**conflict resolution** (when the context layers on balance oppose the trade, it's
downgraded to `watch` rather than firing a weak buy/sell).

```bash
catalyst plan --db oracle.db                                  # propose, print JSON
catalyst plan --db oracle.db --buy-threshold 0.25 --max-age 120 --save
```

Each action carries `action` (`buy`/`sell`/`watch`), `direction`, `confidence`
(0–1), `horizon`, `score`, `catalysts`, `freshness_minutes`, the structured
`layers` map, and a `rationale` string — exactly the per-proposal object in the
deliverable above. With `--save`, actions are appended to an `actions` table (the
audit trail + cooldown source).

> ⚠️ **Proposals only.** The planner never sizes, places, or manages trades.

## Backtest (does the signal predict moves?)

The bridge from "proposals with rationale" to "rules with measured edge." The
harness leans on a property the whole stack shares: every analytic takes a `now`,
so a backtest is just **replaying `now=t` across history** with a strict
point-in-time cut (`indexed_at <= t`), recomputing signals + every bias layer
as-of `t`, and running the **real planner**. Each proposed buy/sell is scored
against what prices actually did over its horizon (`intraday`→24h, `short`→72h).

```bash
catalyst backtest --from 2026-01-01 --to 2026-06-01 --db oracle.db
catalyst backtest --from 2026-05-01 --to 2026-06-01 --weights weights.json --trades
catalyst backtest --from 2026-01-01 --to 2026-06-01 --base-size 0.2 --cost-bps 10
```

It reports in two phases:

- **Phase 1 — signal quality.** Each buy/sell is a unit trade scored on
  directional return: trade count, **hit-rate**, mean/median/cumulative return,
  breakdowns by horizon / catalyst / confidence bucket, a **reliability curve**
  (stated confidence vs realized hit-rate) + `calibration_error`, and a
  buy-and-hold-BTC baseline.
- **Phase 2 — portfolio sim** (`--portfolio`, on by default). Turns those trades
  into an equity curve: each position is **sized by confidence**, charged **fees +
  slippage** (`--cost-bps` per side), and reported as **total return, Sharpe, max
  drawdown, win-rate, profit factor**, and fees paid.

Prices come from the free DefiLlama coins API (cached); unmapped tickers are
skipped and reported, never guessed. The result is only as complete as the
accumulated history. `compare` does the same A/B on weights.

## Tuning

Three levers, all adjustable without touching code:

1. **`enrich --llm`** — the biggest quality jump: a Claude pass gives decisive
   direction and far better asset inference than the free lexicon.
2. **`weights.json`** — every signal + modifier weighting knob. Pass it to
   `signals`/`plan`/`poll` with `--weights`.
3. **`catalyst calibrate`** — don't hand-pick weights: a coordinate-ascent sweep
   over the real backtest (objective = Sharpe / return / hit-rate / calibration)
   writes the winners to `weights.json`.

```bash
catalyst compare --db oracle.db --b weights.json           # default vs candidate
catalyst calibrate --db oracle.db --write weights.json     # tune against the backtest
catalyst poll --llm --model claude-haiku-4-5 --weights weights.json
```

## Data/ML export

With the `[ml]` extra installed, export the stored posts to **Parquet** or
**CSV** (all columns, newest-first), or load them straight into a pandas
DataFrame.

```bash
catalyst export --db news.db --out posts.parquet            # Parquet (default)
catalyst export --db news.db --out posts.csv --format csv
```

```python
from catalyst.store import open_store, to_dataframe

df = to_dataframe(open_store("news.db"), source="rss", limit=1000)
```

## Polling scheduler — the live oracle

`poll` runs the **whole pipeline each cycle**: fetch → save → enrich new posts →
recompute signals → plan (with the persisted cooldown) → **dispatch alerts** →
**record cycle health**. One long-running `poll` keeps the oracle continuously
live and is the natural companion to the Croo provider (the provider delivers the
already-ingested store on payment; `poll` keeps it fresh). Cycles are sequential
and error-resilient; **Ctrl-C** stops after the current cycle.

```bash
catalyst poll                                    # every 5m: fetch → enrich → plan → alert
catalyst poll --interval 30s --buy-threshold 0.25 --primary watcher.guru
catalyst poll --once                             # single cycle (for cron/Task Scheduler)
catalyst poll --llm --model claude-haiku-4-5     # sharper sentiment via Claude each cycle
```

## Alerts & monitoring

An optional `alerts` block turns each cycle's buy/sell proposals into delivered,
de-duplicated notifications: an `AlertRule` (min-confidence, allowed
actions/catalysts, per-asset overrides, quiet hours, cooldown) gates them, then
pluggable **sinks** ship the **canonical JSON payload** — `stderr` (default),
`file` (JSONL), or `webhook` (POST, for Slack/Discord/Telegram/n8n). The alert
cooldown is persisted in SQLite so a repeat is suppressed across restarts, and a
dead sink is logged without stopping the poll loop. **A Croo `deliver_order` is
just another sink over the same payload.**

Every poll cycle also writes a structured `cycle_health` row (timing, per-source
fetch counts, enrich/action counts, any error). A `monitoring` block turns that
history into **ops alerts** — a source going silent for K cycles, an error streak,
a cycle overrunning its interval, or the LLM call budget blown — delivered through
the very same sinks (as `action="ops"`). `catalyst status` prints the one-screen
operator view: last cycle, per-source freshness, open proposals, alert counts, and
any live ops issues. This same health surface is what the Croo provider's
accept/reject gate reads.

## Monitors (named catalyst watches)

Where the alert layer runs **one global rule** over buy/sell proposals, **monitors**
are *named, catalyst-scoped watches* an operator sets up — each with its own match
criteria and its own delivery channel. A monitor is **catalyst-first** and fires on
two independently-selectable trigger paths:

- **`proposal`** — a planner `Action` matched the monitor's assets / catalysts /
  action / confidence / horizon (alert on the *strategy/actions proposed*).
- **`event`** — a freshly-enriched post carrying a watched catalyst on a watched
  asset landed, *before* any full buy/sell proposal (the raw-catalyst trigger,
  e.g. "tell me the moment an AAVE treasury move hits").

Monitors reuse the same pluggable **sinks** as the alert layer, so a monitor can
route to its own webhook (Slack/Discord/Telegram/n8n) or JSONL file; when it names
no sink it falls back to the poll's default sinks. De-dupe is **per-monitor,
per-trigger**, persisted in SQLite (proposals de-dupe on `asset:action` within the
cooldown; events de-dupe on post URI — a one-off catalyst fires once).

```bash
# Watch AAVE treasury moves as raw events → push to a webhook (a Telegram/Discord/Slack bot URL)
catalyst monitor add aave-treasury --catalysts treasury --assets AAVE \
    --on event --webhook https://hooks.example/telegram

# Watch high-confidence unlock-driven SELLs on the proposal path
catalyst monitor add unlock-sells --catalysts unlock --actions sell \
    --on proposal --min-conf 0.6

catalyst monitor list                       # show configured monitors
catalyst monitor check --db catalyst.db     # dry-run the event path over the store (preview)
catalyst monitor rm aave-treasury
```

Monitors live in a CLI-managed `monitors.json` (`--file` to relocate) and are
evaluated each poll cycle — pass `--monitors PATH` to `catalyst poll` (default
`monitors.json`; an absent file simply means no monitors). Emitted events use the
`catalyst.events` payload schema (the raw-catalyst twin of the `catalyst.actions`
deliverable), so a webhook or a Croo delivery treats both identically.

> **Delivery vs. notification.** A *paid Croo order* is delivered in-band via
> `deliver_order` (the buyer agent reads the `catalyst.actions` JSON with
> `get_delivery`) — email/Telegram are **not** part of Croo settlement. Monitors
> and alert sinks are the **human/ops notification** side: Telegram/Discord/Slack
> all work today as `webhook` sinks; a native email (SMTP) sink is an easy add.

## Config (`sources.json`)

```jsonc
{
  "accounts_file": "news-crypto-blue-sky.txt",   // one @handle per line
  "accounts_max": 25,
  "keywords": [ { "q": "mainnet launch", "sort": "latest", "max": 25 } ],
  "feeds": [
    { "url": "https://feeds.bbci.co.uk/news/rss.xml", "max": 25 },
    "https://github.com/ethereum/go-ethereum/releases.atom"   // protocol releases
  ],
  "defillama": {
    "hacks":    { "since_days": 30, "min_amount": 1000000, "max": 50 },
    "tvl":      { "min_tvl": 50000000, "min_change_pct": 15, "window": "1d", "max": 25 },
    "listings": { "days": 7, "min_tvl": 1000000, "max": 25 }
  },
  "macro": { "central_banks": true, "max": 10, "fred": { "api_key": null } },
  "flows": { "etf": true, "assets": ["BTC", "ETH"], "max_days": 14 },
  "onchain": { "unlocks": { "horizon_days": 30 }, "staking": true },
  "onchain_actions": { "watch": [{ "address": "0x…", "asset": "AAVE", "kinds": ["upgrade", "timelock"] }], "min_value_usd": 1000000, "lookback_blocks": 300, "chunk_blocks": 100 },
  "derivs": { "assets": ["BTC", "ETH"] },
  "market": { "fear_greed": true, "limit": 30, "source": "alternative" },
  "dedupe": { "enabled": true },
  "alerts": { "sinks": [{ "type": "stderr" }], "min_confidence": 0.5 },
  "monitoring": { "silence_cycles": 3, "max_error_streak": 3, "llm_call_ceiling": 500 }
}
```

`run`/`poll` fetch every group, de-dupe, and persist them together. Each source is
failure-isolated — a blocked search, dead feed, or DefiLlama hiccup is logged and
skipped, never sinking the batch. A `dedupe` block collapses the same story across
sources to the highest-trust one.

## Normalized record

Every source emits the same `Post` (a pydantic model), distinguished by
`source`. Field names are snake_case:

```jsonc
{
  "source": "bluesky",                  // or "rss", "macro", "flows", …
  "uri": "at://…/app.bsky.feed.post/…", // dedup key
  "url": "https://bsky.app/profile/…",
  "text": "…",
  "created_at": "2026-06-15T…",
  "indexed_at": "2026-06-15T…",
  "author": { "did": "…", "handle": "…", "display_name": "…" },
  "metrics": { "likes": 0, "reposts": 0, "replies": 0, "quotes": 0 },
  "raw": { /* original payload */ }
}
```

Consume it directly from Python:

```python
from catalyst import bluesky, rss
from catalyst.store import open_store, save_posts, query_posts

posts = rss.fetch_feed("https://feeds.bbci.co.uk/news/rss.xml", max=25)
db = open_store("news.db")
save_posts(db, posts)            # -> {"inserted": N, "updated": M, "total": T}
rows = query_posts(db, limit=20)
```

RSS items carry zero `metrics` (feeds have no engagement signal); `uri` is the
entry guid/`<id>` (falling back to the link).

## Tests

```bash
uv run pytest
```

**156 tests, fully offline** (plus one live-sandbox test skipped without Croo
credentials). Coverage spans every layer: Bluesky normalization + cursor
pagination (mocked via `respx`), the SQLite store, the RSS/Atom parser, the
DataFrame/Parquet export (skipped without the `[ml]` extra), enrichment (lexicon +
hybrid candidate routing with a stub LLM), the DefiLlama / Snapshot / macro /
flows / on-chain / on-chain-actions / derivatives adapters, the signal layer, the
planner (decisions, staleness, cooldown-break, conflict, persistence), the
backtest (signal + portfolio phases + reliability), the calibrate optimiser, the
canonical payload + requirements filter, the alerts layer (rules, sinks,
persisted de-dupe), the monitoring layer (health record + issue detection), a
`poll`-cycle integration test, and the **Croo provider** (gate accept/reject,
requirements-filtered delivery, idempotency, full `run()` wiring — the SDK fully
mocked, no network).
