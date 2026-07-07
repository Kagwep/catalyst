# Catalyst — Build Plan: Sources → Planner → Alerts → Monitoring → Croo → Execution

> **Audience: a hired agent.** This is your operating contract. Read it cold and
> you should know the mission, the current state, what to build next, and how to
> verify each piece without asking. Every phase lists concrete files, the
> interface to preserve, the change to make, and a done-check. Work top to
> bottom; do not start a later phase until the earlier one's done-check passes.

## Mission

Catalyst is a **catalyst-driven crypto monitoring oracle**. It ingests many
sources → enriches each item (sentiment + asset + catalyst) → aggregates per-asset
**signals** → a **planner** proposes ranked actions `{asset, action, confidence,
horizon, rationale}`. The oracle **proposes**; it does not size or place trades.

Pipeline today (`README.md:21`):

```
1. INGEST  ✅   2. ENRICH ✅   3. SIGNAL ✅   4. PLANNER ✅   5. BACKTEST ✅
6. ALERTS  ✅   7. MONITORING ✅   8. CROO PROVIDER ✅   9. EXECUTION ◻︎ (gated — see Phase 6)
```

The `poll` loop (`catalyst/cli.py:233`) already runs ingest→enrich→signal→plan each
interval and computes `notable` (buy/sell actions, `cli.py:216`) — but only
**prints them to stderr** (`cli.py:267`). There is no delivery, no de-dupe of
alerts, no health monitoring, no marketplace integration, no execution. That is
what this plan adds.

**Platform target:** the project has been **renamed from `newsOr` to `catalyst`**
(package `catalyst/`, CLI command `catalyst`) and is shaped to run as a **provider
agent on the Croo Network** — a decentralized agent-to-agent service marketplace
with on-chain escrow settlement on Base in USDC. Croo is the storefront + checkout
+ delivery rail; the pipeline is the product. The platform seam is the
`croo-provider` skill (`.claude/skills/croo-provider/`) — read it before Phase 5.

## Ground rules (do not violate)

- **The planner proposes only.** Never auto-place a live trade except behind the
  Phase 6 gate (dry-run default, explicit opt-in, paper before live). Selling a
  *signal* on Croo (Phase 5) is delivery, not execution — it stays a proposal.
- **Preserve interfaces.** `compute_signals()`, `plan()`, and `Action`/`Signal`
  dataclasses are consumed by the backtest harness. Add fields/params with
  defaults; don't break positional signatures.
- **Every source is point-in-time.** Each poll cycle already snapshots bias
  layers (`save_bias_snapshots`, `cli.py:203`) so the backtest can replay
  history. Any new source must snapshot too, or it can't be backtested.
- **No key required wherever possible.** Existing sources favor keyless feeds;
  match that. Gate any keyed source behind config + graceful skip when absent.
- **Tests gate merges.** Add a `tests/test_*.py` for every new module. Run
  `pytest -q` before declaring a phase done.

---

## Phase 1 — Improve news sources

**Goal:** higher signal density and lower latency on the inputs the planner
ranks. Sources live as adapters (`rss.py`, `bluesky.py`, `defillama.py`,
`macro.py`, `flows.py`, `onchain.py`, `market.py`) and are wired through
`run_config()` / `save_posts()` in `cli.py`, configured by `sources.json`.

### 1a. Close the last `◻︎` source: on-chain actions ✅ (2026-07-01)
The one planned-but-unbuilt source (`README.md:44`): contract upgrades, treasury
moves, timelock executions via Etherscan / RPC / webhooks.
- New adapter `catalyst/onchain_actions.py` (keep separate from `onchain.py`, which
  is unlocks+staking supply, per memory `onchain-tier-scope`).
- Output normalized posts with `catalyst="upgrade"|"treasury"|"timelock"` so they
  flow through enrich→signal unchanged. Map address → asset via `protocols.json`.
- Config block `"onchain_actions": { "watch": [...addresses], "min_value_usd": N }`.
- **Done-check:** a known recent upgrade tx appears as an enriched post tagged to
  the right asset; `tests/test_onchain_actions.py` green.

**BUILT 2026-07-01.** `catalyst/onchain_actions.py` reads Ethereum event logs
(`eth_getLogs`, keyless public JSON-RPC) for watched contracts and normalises
proxy `Upgraded` → `upgrade`, TimelockController `CallScheduled`/`CallExecuted`
→ `timelock`, and ERC-20 `Transfer` (optionally `from`-filtered, USD-gated via
the free DefiLlama price join) → `treasury`. Event `topic0`s are **derived**, not
hard-coded, via a pure-Python Keccak-256 validated in tests against the canonical
`Transfer`/`Upgraded` topics. Keyless nodes cap `eth_getLogs` block range
(publicnode ≈100), so scans are **chunked** with a polite inter-window pause and
are fail-soft per window. New catalyst labels `upgrade`/`timelock`/`treasury`
added to the enrich lexicon + `signals.CATALYST_WEIGHTS`. Wired through
`run_config` (`onchain_actions` config block, empty `watch` = no-op default),
the `onchain-actions` CLI subcommand (config-driven or `--address` ad-hoc), and
`sources.json`. Tests `tests/test_onchain_actions.py` (9) green; full suite **108**.
Verified live: real Circle-treasury USDC transfers decode+price+enrich to
`catalyst=treasury`; a real chain `Upgraded` event (proxy `0xb436c6…8229`) →
`catalyst=upgrade`, `$AAVE`. Note: default lookback (300 blk ≈ 1h) suits poll
cadence; wide historical scans need many chunked calls or a higher-limit RPC.

### 1b. Source quality & latency
- **Per-source trust weights are already tunable** (`DEFAULT_SOURCE_WEIGHTS`,
  `signals.py:26`; `PRIMARY_BOOST`). Add a calibration pass: backtest with
  per-source weight sweeps, write the winners to `weights.json`. Don't hand-pick.
- **De-dupe across sources** (same story from Bluesky + RSS + watcher.guru should
  not triple-count strength). Add near-dup detection (title/URL/embedding) at
  `save_posts` time; collapse to one row keeping the highest-trust source.
- **Latency:** `watcher.guru` is the fast primary. Verify the poll `--interval`
  and per-source `max` in `sources.json` aren't starving fast breaking news.

### 1c. New source candidates (rank by signal/effort, build top 1–2)
- Funding-rate / OI feeds (derivatives positioning → regime input).
- Exchange listing announcements (direct, not just DefiLlama-derived).
- Large-transfer / whale-alert style on-chain flow.
Each must: normalize to a post, carry a `catalyst`, snapshot if it's a bias layer.

**Phase 1 done-check:** `catalyst poll --once` ingests every configured source
without error; new sources produce enriched, asset-attributed posts; `pytest -q`
green; a backtest run shows non-degraded P&L vs. the pre-Phase-1 baseline.

**BUILT 2026-07-01 (1b + 1c):**
- **1b de-dupe** — `catalyst/dedupe.py`: collapses the same story across sources
  to the highest-trust member (canonical-URL match or title-token Jaccard,
  dependency-free, precision-biased). Wired into `run_config` (`dedupe` config
  block, default on). `tests/test_dedupe.py`.
- **1b calibration** — `catalyst/calibrate.py` + `calibrate` CLI: coordinate-ascent
  sweep of the modifier weights over the real backtest, objective =
  Sharpe/return/hit-rate/calibration; writes winners to `weights.json`
  (`modifier_weights`, now honoured by `plan`/`poll` via `_modifier_weights`). Pure
  optimiser unit-tested with a stub backtest. `tests/test_calibrate.py`.
- **1c derivatives source** — `catalyst/derivs.py`: keyless Binance perp funding +
  open interest → per-asset **positioning bias** (crowded longs fade bullishness /
  crowded shorts fade bearishness). Full bias-layer wiring parallel to market:
  `store.fetch_derivs`, `save_bias_snapshots`, `pipeline._derivs_sources`, backtest
  `replay`/`run_backtest` (`derivs_kwargs`), planner `derivs_bias`/`derivs_weight`,
  `derivs`/`derivsbias` CLI, `plan`/`poll` `--derivs`, `weights.json funding_scale`.
  Text uses the exchange symbol so it never leaks into the signal layer. Live-
  verified on Binance. `tests/test_derivs.py`. (The `onchain_actions` treasury/whale
  read already covers 1c's large-transfer candidate.)

---

## Phase 2 — Improve the planner

**Goal:** better-calibrated, less noisy proposals. Planner is `plan()` in
`catalyst/planner.py`. Confidence today = `0.6*|score| + 0.4*strength + cat_bonus`,
then multiplied by macro/flow/supply/market modifiers (`planner.py:156`).

### 2a. Calibrate confidence against outcomes
- The backtest already scores proposals on realized prices. Use it to check
  whether stated `confidence` predicts hit-rate (reliability curve). If a
  confidence of 0.7 doesn't win ~70% of the time, **recalibrate the formula or
  the modifier weights** — don't leave confidence as an unanchored number.
- Treat `macro_weight / flow_weight / supply_weight / market_weight` (defaults
  0.3/0.25/0.25/0.25) as tunable; sweep them in the backtest, persist winners.

### 2b. Tighten the gates
- **Staleness** (`max_age_minutes`) downgrades stale buy/sell to `watch`
  (`planner.py:147`). Verify the threshold per horizon — intraday catalysts
  (`_FAST_CATALYSTS`) should expire faster than `short`.
- **Cooldown** (`cooldown_minutes`, default 120) suppresses repeat asset+action.
  Confirm it doesn't suppress a *strengthening* signal (rising score / velocity)
  — consider letting a materially higher confidence break cooldown.
- **Conflict resolution:** if two layers disagree hard (bullish sentiment vs.
  bearish flows), the modifier math fades confidence — confirm that produces a
  `watch`, not a low-confidence `buy` that still fires an alert.

### 2c. Explainability for the alert payload
- `rationale` (`planner.py:50`) is already a readable string with the per-layer
  notes. Ensure it's complete enough that an alert recipient needs no other
  context: asset, action, confidence, horizon, the catalysts, and which layers
  pushed which way.

**Phase 2 done-check:** reliability curve plotted from a backtest; modifier
weights tuned and persisted; cooldown/staleness behavior covered by
`tests/test_planner.py`; `pytest -q` green.

**BUILT 2026-07-01 (2a + 2b + 2c):**
- **2a reliability + calibration** — backtest now emits a `reliability` curve
  (stated confidence vs realized hit-rate per bucket) + `calibration_error`; the
  1b `calibrate` sweep tunes/persists modifier weights (metric `calibration`
  minimises the gap). `tests/test_backtest.py`, `tests/test_calibrate.py`.
- **2b gates** — planner now: expires **fast/intraday** catalysts sooner than
  `short` (`fast_max_age_minutes`, default 60m in CLI); **breaks the cooldown** for
  a materially-more-confident repeat (`cooldown_break_delta`, needs the prior
  confidence — added to `fetch_recent_actions`); and **downgrades to `watch`** when
  the modifier layers on balance oppose the trade (`conflict_margin`) instead of
  emitting a weak buy/sell. `tests/test_planner.py`.
- **2c explainability + the Croo seam** — the four modifier blocks are unified into
  one loop that emits a structured `Action.layers` map (per layer: label/bias/
  effect/weight) alongside the human `rationale`. `catalyst/payload.py` is the
  single canonical `Action[]`→JSON deliverable both a Phase-3 webhook sink and the
  Phase-5 Croo `deliver_order` serialise through (versioned, disclaimer baked in),
  plus `select_actions`/`requirements_to_kwargs` — the buyer-requirements filter the
  Phase-5 service contract is a thin wrapper over. `tests/test_payload.py`.

**Full suite: 135 passing.** Design rule held for Phase 5: every outward payload
goes through `payload.build_payload`, so wiring Croo is "call it, ship the dict."

---

## Phase 3 — Alerts (delivery layer)

**Goal:** turn `notable` actions into delivered, de-duplicated notifications.
This is the first genuinely-new subsystem.

### 3a. Alert model & rules
- New module `catalyst/alerts.py`. An **AlertRule** decides if an `Action` warrants
  delivery: `min_confidence`, allowed actions (buy/sell/watch), allowed catalysts,
  per-asset overrides, quiet hours. Config in `sources.json` under `"alerts"`.
- **De-dupe / cooldown at the alert layer** (separate from planner cooldown): an
  alert for `(asset, action)` already delivered within N minutes is suppressed;
  persist alert history in SQLite (`store.py`) so de-dupe survives restarts.

### 3b. Sinks (pluggable)
- A `Sink` interface: `send(alert) -> ok`. Implement, in order of value:
  1. **stdout/file** (always-on, the default, replaces today's stderr print).
  2. **Webhook** (POST JSON — works for Slack/Discord/Telegram bots/n8n).
  3. Optional native Telegram/Discord if a token is configured.
- Each sink configured + independently togglable; a sink failure must **not** kill
  the poll loop (wrap like the existing per-cycle try/except, `cli.py:273`).
- **Design the payload once.** The alert payload IS the Croo deliverable (Phase 5):
  define a single `Action[]`-derived JSON shape here so a webhook push and a paid
  `deliver_order` emit the same structure. A Croo delivery is just another sink.

### 3c. Wire into poll
- Replace the stderr print block (`cli.py:267`) with `alerts.dispatch(notable)`.
  Keep stderr as the stdout-sink default so existing behavior is preserved when
  no sinks are configured.

**Phase 3 done-check:** `catalyst poll` delivers a buy/sell over a configured
webhook; a duplicate within the cooldown is suppressed; alert history is in
SQLite; sink failure logs and the loop continues; `tests/test_alerts.py` green.

**BUILT 2026-07-01.** `catalyst/alerts.py`: `AlertRule` (min_confidence, allowed
actions/catalysts, per-asset overrides, quiet hours, cooldown) + pluggable
`Sink`s (`StderrSink` default, `FileSink` JSONL, `WebhookSink` POST) + `dispatch`
(rule filter → alert-layer de-dupe against the SQLite `alerts` table so repeats
are suppressed across restarts → deliver fail-soft per sink → record on success).
**Every sink emits `payload.build_payload` — the same canonical dict a Croo
`deliver_order` will send, so a Croo delivery is literally just another `Sink`.**
`build_alerting(cfg)` builds rules+sinks from the `alerts` config block (default =
one stderr sink delivering buy/sell = prior behaviour). Wired into `_cmd_poll`
(replaces the inline stderr print; `dispatch(notable, …)` each cycle). Store:
`alerts` table + `save_alerts`/`fetch_recent_alerts`. `tests/test_alerts.py` (7);
full suite **142**. Live-verified: real `WebhookSink` POST to httpbin → 200.

---

## Phase 4 — Monitoring (operational health)

**Goal:** know the oracle itself is healthy — distinct from the trading signal.
A hired agent must be able to prove it's running, not just claim it.

### 4a. Per-cycle health record
- Persist a `cycle_health` row each poll: timestamp, duration, per-source
  fetched/inserted counts, per-source error, items enriched, actions/notable
  counts. The cycle summary (`cli.py:266`) already has most of this — capture it
  structured, not just printed.

### 4b. Liveness & staleness alerts
- **Source went silent:** if a source returns 0 new items for K cycles when it
  normally produces, emit a *monitoring* alert (reuse Phase 3 sinks, a separate
  `ops` channel/rule).
- **Loop stalled / erroring:** N consecutive cycle errors, or cycle duration
  blowing past the interval, raises an ops alert.
- **API budget:** if the LLM scorer (`make_anthropic_scorer`, `cli.py:241`) is
  on, track call count / cost per cycle and alert on a ceiling.

### 4c. Status surface
- `catalyst status` subcommand: last cycle time, per-source freshness, open
  proposals, alert counts, error streak. One screen an operator (or you) can read.
- Optional: a tiny `/healthz` if run as a service (defer unless asked).

**Phase 4 done-check:** killing a source feed raises an ops alert within K
cycles; `catalyst status` reflects reality; health rows accumulate in SQLite;
`tests/test_monitoring.py` green.

**BUILT 2026-07-01.** `catalyst/monitoring.py` + store `cycle_health` table.
`_poll_cycle` now returns a structured `CycleHealth` (timing, per-source fetch
counts, enrich/LLM/action counts, error); `_cmd_poll` times each cycle, persists
it (`save_cycle_health`), then runs `detect_issues` over the accumulated history:
**source_silent** (a source at 0 for K cycles), **error_streak** (N erroring
cycles), **slow_cycle** (duration > 1.5× interval), **llm_budget** (calls over a
ceiling). Issues become `action="ops"` `Action`s delivered through the **same
Phase-3 sinks** under a separate `OPS_RULE` (de-dupe + fail-soft reused). `catalyst
status` prints the operator screen (last cycle, per-source freshness, open
proposals, alert counts, error streak, live ops issues). Config: `monitoring`
block (silence_cycles/max_error_streak/llm_call_ceiling). `tests/test_monitoring.py`
(8); full suite **150**. Live-verified: a source going silent for 3 cycles raised a
`source_silent` ops alert through a sink and flipped `status.healthy` to false.

---

## Phase 5 — Croo provider (sell the signal on the marketplace)

**Goal:** run Catalyst as a **provider agent on the Croo Network** — register a
service, accept paid orders, and deliver the planner's output as a structured
result. This is what the rename and the whole platform tailoring are for. **Read
the `croo-provider` skill first** (`.claude/skills/croo-provider/SKILL.md` +
`reference.md`) — it has the verified SDK surface and gotchas.

**Depends on:** Phase 3 (the delivery payload) and Phase 4 (the health gate).

> The Croo SDK (`croo.AgentClient`, at `D:\projects\python-sdk`) is **runtime
> only**. Agent creation, **service registration**, SDK-Key issuance, and funding
> the agent's **AA wallet** with USDC happen in the Croo Dashboard — not in code.

### 5a. No-op provider (prove the rail before wiring the product)
- New module `catalyst/croo_agent.py`. Stand up the async event loop against the
  SDK: `connect_websocket()` → on `NEGOTIATION_CREATED` accept → on `ORDER_PAID`
  deliver a **hardcoded** JSON → confirm `ORDER_COMPLETED`. Prove auth
  (`CROO_SDK_KEY`) and the loop end-to-end on testnet/sandbox first.
- Config: `CROO_API_URL`, `CROO_WS_URL`, `CROO_SDK_KEY`, optional `BASE_RPC_URL`.
- **One EventStream per key** (duplicate-key WS gets a 1008 and won't reconnect).

### 5b. Service contract (the product definition)
- **Requirements schema** (buyer input, `requirements_type=schema`): a typed JSON
  object, e.g. `{ "assets": [...], "horizon": "intraday|short", "min_confidence": N }`.
  Arrives on `Negotiation.requirements`.
- **Deliverable: `DeliverableType.SCHEMA`** — deliver the `Action[]` payload
  defined in Phase 3b as JSON. Keep the proposal disclaimer in it.
- **Pricing:** flat USDC per call (standard model). Leave **Require Fund Transfer
  OFF** — that model is for moving principal (Phase 6 only).
- **SLA** (`sla_hours`/`sla_minutes`): set above worst-case pipeline run time
  (use the Phase 4 cycle-duration record), or orders auto-refund and reputation
  drops.

### 5c. Wire the pipeline + gate
- Replace the hardcoded delivery with a real run: on `ORDER_PAID`, parse
  requirements, run the pipeline (the sync engine via `asyncio.to_thread` so it
  doesn't block the WS heartbeat), map `Action[]` → the deliverable schema,
  `deliver_order`.
- **Accept/reject gate** (`accept_negotiation` mints an on-chain order — gate it):
  reject when requirements are unparseable, the asset universe is uncovered, or
  the Phase 4 health surface says the pipeline is stale/unhealthy.
- **Idempotency:** make the `ORDER_PAID` handler guard on order status / a
  delivered-set — a reconnect can redeliver an event; never double-run or
  double-deliver.

### 5d. Integration mode
- Default to **order-driven** (run on payment, deliver fresh per call). Defer the
  **standing/subscription** mode (keep polling, deliver latest) until there's
  demand for a feed.

**Phase 5 done-check:** a requester order is accepted, paid, fulfilled with a
real pipeline-derived `Action[]` deliverable, and reaches `ORDER_COMPLETED` on
sandbox; an out-of-scope/unhealthy negotiation is rejected with a reason; the
handler is idempotent under a forced reconnect; `tests/test_croo_agent.py` green
(mock the SDK — don't hit the network in unit tests).

**BUILT 2026-07-01.** `catalyst/croo_agent.py` — `CrooProvider` async event loop
over `AgentClient`. `NEGOTIATION_CREATED` → `gate()` (parse requirements +
coverage + Phase-4 `default_health`) → `accept_negotiation` or
`reject_negotiation(reason)`. `ORDER_PAID` → idempotency guard (local delivered-set
+ on-chain order status) → `default_pipeline` in `asyncio.to_thread` (signals →
biases → planner → `select_actions(**requirements_to_kwargs(req))` →
`build_payload`) → `deliver_order(DeliverableType.SCHEMA, json)`. All `croo` imports
are lazy (SDK not a hard dep, not installed here); pipeline/health/deliver_factory
are injectable seams so tests fully mock the SDK. Order-driven mode (5d default);
standing/subscription deferred. CLI `croo-provider` (`--assets` coverage; reads
`CROO_API_URL/CROO_WS_URL/CROO_SDK_KEY/BASE_RPC_URL`). `tests/test_croo_agent.py`
(9); full suite **159**. Verified: gate accept/reject, requirements-filtered real
delivery, idempotency under redelivery + already-past-paid status, and the full
`run()` wiring (faked stream) accepting then delivering.

**Live-sandbox leg is operator/Dashboard-side (SDK is runtime-only):** create the
agent, register the service (`requirements_type=schema`, `deliverable_type=schema`,
Require Fund Transfer OFF, `sla_hours` above worst-case cycle duration), issue the
SDK-Key, fund the agent **AA wallet** with USDC — then `catalyst croo-provider`
listens and fulfils. `ORDER_COMPLETED` settles escrow to the AA wallet.

### 5e. Live-smoke findings (2026-07-04) + usability backlog

> **PAUSED 2026-07-04.** Live smoke test postponed; provider switched off (no
> agent online). **State when we left off:** deliverable fixed to match the
> registered single-object schema and verified (no-op payload has every required
> field; 174 tests green) — but **not yet confirmed end-to-end against prod** (the
> corrected delivery hasn't completed a live order). **To resume:** start the
> provider (`catalyst croo-provider --no-op`, needs `.env` loaded) so the agent
> goes online, then place a fresh order from the second/buyer agent; watch for
> accept → deliver → `ORDER_COMPLETED`. Address the usability backlog below first
> if reworking the requirements form.


Reframed the deliverable to **watch signals** (no buy/sell/hold): `catalyst.signals`
v2.0 — `signal` (alert|watch) + `direction`, filters on the same. First live smoke
against prod surfaced these (there is **no testnet** — Base mainnet only, gas
sponsored):

- **Self-order is blocked** — `negotiate_order` on your own service → `cannot
  negotiate own service`. The full round-trip needs a **second, funded buyer agent**.
- **Provider must be online** — the agent shows offline until our process holds the
  WS open; a buyer can't be fulfilled otherwise.
- **Registered schema is ground truth** — the Dashboard builder can't do
  arrays-of-objects, so the service was registered with `actions` as a **single
  flat object** (`asset` a field, `freshness` not `freshness_minutes`), `catalysts`
  a flat array, `universe` required. `flatten_signals` now emits exactly this (the
  top signal by confidence). A mismatch → `INVALID_DELIVERABLE`, the order sits, and
  the **SLA expires + auto-refunds** (no funds lost, but the smoke fails).

**Usability backlog (before real buyers):**
- **Requirements form is too bare.** The `requirements_type=schema` fields render as
  simple questions with no explanation — a buyer sees `signal` / `direction` /
  `horizon` with no idea what they entail (alert vs watch? what a horizon means?
  the 0–1 confidence range). Add per-field **descriptions / help text / enum
  meanings** to the registered requirements schema so it's self-explanatory. Treat
  the buyer as knowing nothing about Catalyst's internals.
- **Test with a descriptive service name.** Register/exercise the service under a
  clear, buyer-facing **descriptive name** (not a codename) and walk the order flow
  as a first-time buyer would, to check the listing + requirements form are
  understandable end-to-end. Usability is part of the done-check, not just a
  successful `ORDER_COMPLETED`.
- **Empty-signal delivery.** The registered schema requires the `actions` fields;
  if the real pipeline produces **no** signal for the requested asset, delivery
  would fail those required fields. Decide the contract: deliver a neutral
  `watch`/`no-signal` object for the requested asset, or document that empties are
  possible — don't let it silently `INVALID_DELIVERABLE` → SLA-expire.

---

## Phase 6 — Execution (GATED — do not build without explicit sign-off)

**Status: out of scope by default** (`README.md:26`, `planner.py:9`). The oracle
proposes; sizing/risk/execution are the operator's. Build this **only** when the
user explicitly authorizes live or paper execution, and even then:

- **Dry-run is the default and only default.** Live trading requires an explicit,
  per-run opt-in flag *and* a config secret — never a silent default.
- **Paper before live.** Ship a paper-trading executor that records intended
  fills against live prices first; prove it against the backtest's assumptions.
- **Hard risk rails, enforced in code, before any venue call:** max position
  size, max concurrent exposure, per-asset cap, daily loss kill-switch, and a
  global kill-switch the monitoring layer can trip.
- **Separation:** execution reads `Action`s; it never re-derives signals. An
  `Executor` interface mirrors the `Sink` pattern (paper, then one venue).
- **Croo angle:** this is the only place the fund-transfer pricing model
  (`require_fund_transfer`, `accept_negotiation_with_fund_address`,
  `provider_fund_address`) applies — a service that *moves principal*, not just
  delivers a signal. Still gated; still opt-in.
- **Audit:** every intended and actual order persisted, reconcilable to the
  proposal that caused it.

**Do not start Phase 6 from this document alone.** Confirm scope with the user
first. The earlier `eventual_task.txt` framing explicitly removed the execution
layer in favor of a strategy-spec deliverable — that pivot may still hold.

---

## Phase 7 — Trend layer & multi-day horizon (history-driven)

**Goal:** turn the point-in-time bias history the poll already records into (a) a
new **trend modifier** and (b) a longer **`swing` horizon**, so the oracle can say
not just *where* an asset's bias is but *which way it's been moving over days*.

**Substrate already exists.** Every poll cycle writes `save_bias_snapshots(...)`
for all five layers with a UTC `ts` (`cli.py:256`); `fetch_bias_snapshots()` reads
them back (`store.py:274`), indexed `(layer, asset, ts)`. Nothing to build for
storage — the phase is blocked only on *accumulating* history (7a) and then
*reading* it (7b/7c).

### 7a. Continuous polling + shared store (prerequisite)

> **STATUS 2026-07-07 — hosting DEFERRED.** Run everything **locally on SQLite for
> now**; host later. The `7a-store` Postgres seam below is **SHELVED until we
> actually host** — do not build it yet. Topology decision is deferred but
> **leaning co-located one-box** (poller + provider on one ~$5/mo box, local
> SQLite), which would drop the seam entirely; separated + Postgres only earns its
> keep at scale (see [[hosting-topology]]). **Immediate focus: poller + DB +
> integration, locally.**

- The trend features are meaningless without days of snapshots. Stand up an
  always-on loop: `catalyst poll --interval 15m`, **no `--llm`** (I/O-bound, no
  model cost — the smallest always-on box suffices). Cadence 10–15m, not faster:
  ETF flows/macro are daily-hourly and Bluesky rate-limits (already 403s).
- **Separated (operator preference):** the poller and the Croo provider run as
  **separate processes/hosts**. The poller *writes* snapshots/posts/actions; the
  provider *reads* them at delivery time (existing layers + the new trend layer).
  So they must share ONE store — local-disk SQLite no longer works across hosts.
  - Introduce a store backend both reach: **managed Postgres** (recommended) or a
    shared network volume. Abstract the `sqlite3`-specific calls in `store.py`
    behind a connection/dialect seam; keep the schema identical (WAL is moot on PG).
  - Retention: keep `bias_snapshots` indefinitely (tiny, numeric); prune old
    `posts.text` if size matters. Point-in-time reads must filter `ts <= now`.
- **Done-check:** the provider delivers a trend-aware signal computed from history
  written by a *physically separate* poller against the shared store.

#### 7a-store. SQLite → Postgres seam (concrete)

`store.py` is stdlib `sqlite3`, ~10 SQL statements, no ORM. The seam keeps the raw
SQL and swaps only the driver + the handful of dialect-specific constructs. Add
`psycopg` (v3) as an **optional** dep (`catalyst[postgres]`), lazily imported like
the croo SDK — the `sqlite` default stays dependency-free.

**Dialect inventory (everything that differs — audited 2026-07-07):**

| Construct | SQLite (today) | Postgres |
|---|---|---|
| Connect + rows | `sqlite3.connect`; `row_factory=sqlite3.Row` | `psycopg.connect(dsn)`; `row_factory=dict_row` |
| PK / id | `INTEGER PRIMARY KEY AUTOINCREMENT` ×5 | `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` |
| Journal | `PRAGMA journal_mode=WAL` (`store.py:143`) | drop (no-op) |
| Schema load | `executescript(_SCHEMA)` | psycopg3 runs multi-stmt in one `execute`, or split |
| Param style | **MIXED**: `:name` (SELECTs) **and** `?` (writes) | `%(name)s` / `%s` |
| Upsert | `ON CONFLICT(uri) DO UPDATE SET x=excluded.x` | **identical** (`EXCLUDED`) — `uri` is a real PK ✅ |
| Column migration | `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` (`:149`) | `ALTER TABLE … ADD COLUMN IF NOT EXISTS` |
| Scalar read | `.fetchone()[0]` (`_count :159`) | breaks under `dict_row` — make row-factory-agnostic |
| Timestamps | ISO **TEXT**, lexically sorted | **keep TEXT** (not `timestamptz`) so `ts<=:before` / `>=:cutoff` compares identically |
| Types | `REAL`, `TEXT`, `raw` JSON-as-TEXT | `DOUBLE PRECISION`/`REAL`, `TEXT` (keep TEXT; `JSONB` later, optional) |

**Work items:**
1. **URL-driven `open_store(url)`** (default `sqlite:///catalyst.db`, or
   `postgresql://…`). Return a thin `Store` wrapper exposing `.execute`,
   `.executemany`, `.transaction()`, `.close()` so callers never touch the raw
   driver. Thread a `STORE_URL` env + `--store` CLI flag through poll + croo_agent;
   keep `--db PATH` as an alias for `sqlite:///PATH`.
2. **Standardize param style on named `:name`** — convert the ~8 `?`-qmark writes
   (`save_enrichments :214`, `save_actions :238`, `save_bias_snapshots :268`,
   alerts `:349`, monitor_fires `:382`, actions-insert `:303`) to named params.
   Then the translator is one safe rule (`:name` → `%(name)s`); avoids the
   error-prone `?`→positional path. (No `:` appears inside any string literal in
   this module, so translation is unambiguous.)
3. **`schema_sql(dialect)`** — templatize the two DDL differences (identity PK,
   drop PRAGMA); `CREATE TABLE/INDEX IF NOT EXISTS` is valid in both.
4. **`add_column_if_missing()`** — branch: sqlite `PRAGMA table_info`, PG native
   `ADD COLUMN IF NOT EXISTS`.
5. **Fix `_count`** (and any `[0]` row indexing) to read by position
   independent of row factory.
6. **Provider connection lifecycle** — `croo_agent` opens/closes a store **per
   order**; on PG that's a TCP+auth per order. Low volume: fine. Otherwise add a
   small `psycopg_pool.ConnectionPool` held by the provider.
7. **One-time data copy** (optional) — `catalyst store-migrate sqlite:///old
   postgresql://…` bulk-copies each table for history continuity; OR start PG
   fresh and let the hosted poll re-accumulate (macro backfills via FRED history,
   flows self-accumulate — see [[backtest-and-history]]).

**Testing:** parametrize the `store`/`history` test fixtures on `STORE_URL` so the
same assertions run against both backends (PG via a disposable test DB /
testcontainers in CI). Point-in-time (`ts<=t`), upsert, and additive-migration
behavior must match on both.

**Alternative considered — SQLAlchemy Core:** erases the param-style + DDL churn
and gives pooling for free, but means rewriting the raw SQL into Core constructs
and a heavier dep. Given the module is ~10 statements and the project prizes low
deps, the thin seam wins now; revisit Core only if the schema grows materially.

### 7b. Trend layer (new per-asset modifier) ✅ BUILT 2026-07-07

> **Done:** `catalyst/trend.py::compute_trend_bias` (OLS slope×span of a layer's
> bias over `window_days`, → `TrendBias(asset,bias,label,evidence)`, v1 layer=flows,
> point-in-time `ts<=now`, thin-history omitted). Wired as the `("trend", …)`
> modifier in `planner.plan` (surfaces in `layers.trend`), computed in both call
> sites (`_poll_cycle`, `default_pipeline`), `trend_weight` (0.25) in
> `_modifier_weights`. Tests: `tests/test_trend.py` (6 — rising/falling/flat,
> thin-omit, point-in-time, planner boost/damp). Suite 183✅. Live-verified against
> the accumulating DB (flat until days of history bank). **Remaining: 7c.**

- New `catalyst/trend.py`: `compute_trend_bias(conn, assets, *, window_days=7,
  now=…) -> {asset: Bias(bias, label)}`, mirroring `compute_flow_bias` et al.
  Reads `fetch_bias_snapshots(asset, since=now-window)`, builds a per-`ts`
  aggregate directional bias, and returns the **normalized slope** over the window
  scaled to `[-1, 1]`. `label`: `strengthening` (rising) | `weakening` (falling) |
  `flat`.
  - **v1 simplification:** trend a single most-informative layer — **flows**
    (multi-day institutional accumulation is the canonical multi-day signal) —
    then generalize to a composite. Document both; ship flows-slope first.
- **Wiring is trivial** — it's just one more entry in the planner's modifier list
  (`planner.py:187`): `("trend", label, bias, trend_weight)`. It then flows through
  the SAME confidence / `net_align` / `layers` / conflict machinery (`:200-206`) —
  no new apply path. Add `trend_weight` to `weights.json` + `_modifier_weights`.
- **Guards:** require ≥K snapshots spanning ≥ a window fraction before emitting a
  trend modifier (cold-start / thin history → *no* modifier, never a spurious
  `flat`). Read only `ts <= now` (backtest safety).
- **Surface:** appears in `layers.trend` like any modifier; optionally add a
  top-level `trend_7d` field to the deliverable. New test: a rising history boosts
  an aligned buy, a falling one damps it; backtest shows no P&L regression.

### 7c. Multi-day horizon tier ✅ BUILT 2026-07-07

> **Done:** horizon vocab is now `intraday | short | swing`. `planner.plan` promotes
> non-fast `short → swing` when `|trend_bias| >= swing_trend_threshold` (0.2); fast
> catalysts still win → intraday. Three-tier staleness: new `swing_max_age_minutes`
> (looser) gate, threaded via `--swing-max-age` (default 7d) on `poll`/`plan`.
> Enum propagated: deliverable `horizon` (passthrough), `backtest`
> `DEFAULT_HORIZON_HOURS[swing]=168`, Croo requirements `horizon` enum
> (`reference.md`). Verified `swing` flows planner→payload→flattened Croo shape.
> Tests: `test_planner.py` (swing-on-trend, fast-wins, looser-staleness). Suite
> 185✅. **Phase 7 complete except the shelved 7a-store (deferred to hosting).**

- Extend the horizon vocabulary `intraday | short` → `intraday | short | **swing**`
  (multi-day / weeks).
- **Classification** (`planner.py:164-167`): keep `fast → intraday`. For non-fast
  signals, promote `short → swing` when the trend layer shows a **persistent**
  multi-day move — `|trend bias| ≥ threshold` AND sign stable across the window
  (not a one-cycle blip). Otherwise stays `short`.
- **Staleness:** `swing` tolerates older data — add `swing_max_age` (≫ short) so a
  multi-day setup isn't killed by intraday freshness gates (`planner.py:171`
  becomes a three-tier `age_limit` selection).
- **Propagate the enum everywhere it's constrained:** `action_to_dict` /
  deliverable `horizon`; the Croo **requirements** `horizon` enum (`reference.md` +
  Dashboard help text: `intraday`=hours, `short`=days, `swing`=multi-day/weeks);
  `select_actions` horizon filtering (already generic); the backtest holding period
  (`swing` holds across days).
- **Done-check:** a persistent multi-day trend yields a `swing` action held across
  days in the backtest, and the enum round-trips through the Croo requirements filter.

### 7d. Build order
- **7a is the hard prerequisite for *real* signal**, but **7b/7c can be built and
  tested first against synthetic `bias_snapshots` fixtures** — they only need the
  read shape, not live history. Recommended: 7b + 7c against fixtures (fast, fully
  unit-testable) → stand up 7a → let history bank → validate live. This keeps the
  planner work independent of the hosting/DB-migration work.

### 7e. Calibrate trend/swing params (FOLLOW-UP — after ~1 week of banked history)

7b/7c shipped with **guessed defaults** — no history existed to tune against. Once
the local poller (7a) has banked ~1 week of `bias_snapshots`, calibrate:
- `trend.py`: **`window_days`** (7), **`flat_threshold`** (0.1), **`min_points`** (3)
  — do real flows-bias slopes over the window separate signal from noise?
- `planner.plan`: **`swing_trend_threshold`** (0.2) — does it fire `swing` on genuine
  multi-day moves without over-promoting? **`trend_weight`** (0.25) — right pull vs
  the other layers? **`swing_max_age_minutes`** (7d) — sane hold-open window?
- **Method:** use the existing backtest (`swing`→168h hold already wired) to sweep
  these against realized returns, like the other modifier weights (the
  `calibrate`/`compare` loop — see [[backtest-and-history]]). Fold tuned values into
  `weights.json` (`modifier_weights.trend_weight`) + a `trend`/`swing` config block.
- **Trigger:** revisit when `SELECT count(DISTINCT ts) FROM bias_snapshots` covers
  ≥~5–7 days per asset and `compute_trend_bias` starts returning non-`flat` labels.

---

## Sequencing & how to pick up work

1. Phases are ordered by dependency: **1 → 2 → 3 → 4 → 5**, with **6 gated** and
   **7 additive**. (Phase 5/Croo needs 3's payload and 4's health gate; it can
   start once those land. Source/planner work, 1–2, is independent of the platform.
   **Phase 7** — trend/multi-day horizon — reuses the existing `bias_snapshots`
   history: 7b/7c are buildable now against fixtures; 7a (hosted continuous poll +
   shared store) is what makes them *real*.)
2. Within a phase, do the lettered items in order; each has its own done-check.
3. Before starting, run `pytest -q` to confirm a green baseline.
4. After each item: add/extend its test, run `pytest -q`, and (for source/planner
   work) run a backtest to confirm no P&L regression.
5. Keep this file current: when an item lands, mark it and note the commit/PR.

## Quick file map

| Concern | File |
|---|---|
| Poll loop / CLI wiring | `catalyst/cli.py` (`_poll_cycle` ~170, `_cmd_poll` ~233) |
| Sources | `rss.py` `bluesky.py` `defillama.py` `macro.py` `flows.py` `onchain.py` `market.py` |
| Enrichment | `enrich.py` |
| Signals | `signals.py` (`compute_signals`) |
| Planner | `planner.py` (`plan`, `Action`) |
| Backtest | `backtest.py`, `tests/test_backtest.py` |
| Persistence | `store.py`, `snapshot.py` |
| Config | `sources.json`, `weights.json`, `protocols.json` |
| **New: alerts** | `catalyst/alerts.py` (Phase 3) |
| **New: monitoring** | `catalyst/monitoring.py` + `status` cmd (Phase 4) |
| **New: Croo provider** | `catalyst/croo_agent.py` (Phase 5) + `croo-provider` skill |
| Croo SDK (reference) | `D:\projects\python-sdk` (`croo.AgentClient`, async) |
| **New: execution** | `catalyst/execution.py` (Phase 6, gated) |
| **New: trend layer** | `catalyst/trend.py` (Phase 7) — reads `bias_snapshots`, feeds `planner` modifiers |
| Bias history | `store.py` (`save_bias_snapshots` ~248, `fetch_bias_snapshots` ~274) |
