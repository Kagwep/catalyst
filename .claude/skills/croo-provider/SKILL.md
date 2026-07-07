---
name: croo-provider
description: Wire the Catalyst oracle to run as a provider agent on the Croo Network (decentralized agent-to-agent service marketplace, on-chain settlement on Base in USDC). Use when building, registering, or operating Catalyst as a sellable Croo service — listening for orders, running the pipeline on payment, and delivering structured signal/strategy results, or when reasoning about how the project should be shaped to fit the Croo SDK (croo.AgentClient).
---

# Croo Provider Skill

How **Catalyst** (this project — renamed from `newsOr`) sells its catalyst
signals as a service on the **Croo Network**: a decentralized marketplace where
AI agents buy and sell services agent-to-agent, with on-chain escrow settlement
on **Base** in **USDC**. Catalyst runs as a **provider agent**.

> Mental model: the existing pipeline (ingest → enrich → signal → planner →
> backtest) is the **product**. Croo is the **storefront + checkout + delivery
> rail**. This skill is the seam between them. The full SDK API surface is in
> `reference.md` — read it before writing client code.

## What Croo gives us, and what it doesn't

- **Gives:** machine-native identity (agent DID), discoverable service listing,
  order negotiation, on-chain escrow, USDC settlement, SLA-enforced auto-refund,
  reputation, file storage, a real-time WebSocket event stream.
- **Does NOT give:** the signal itself. Catalyst's pipeline is the alpha; Croo
  only transports and monetizes it.
- **Setup is Dashboard-side, not SDK.** Agent creation, **service registration**,
  SDK-Key issuance, and funding the agent's AA wallet all happen in the Croo
  Dashboard. The SDK (`croo.AgentClient`) is **runtime only**. Do not try to
  register a service from code — it isn't in the SDK.

## The provider lifecycle (what Catalyst implements)

The whole job is an async event loop over `AgentClient`. One service, one client.

```
connect_websocket()
  │
  ├─ on NEGOTIATION_CREATED ─► inspect requirements → accept_negotiation()  (creates on-chain order)
  │                                                 └ or reject_negotiation(reason)
  │
  ├─ on ORDER_PAID ──────────► RUN THE CATALYST PIPELINE → deliver_order(SCHEMA json)
  │
  └─ on ORDER_COMPLETED ─────► escrow settles to our AA wallet (auto on delivery — no buyer sign-off)
```

> **Settlement is automatic on delivery.** A valid `deliver_order` records the
> deliverable's keccak256 hash on-chain and the escrow auto-splits (platform fee
> → Treasury, remainder → our AA wallet); `ORDER_COMPLETED` is the *notification*
> of that, not a buyer acceptance step. There is **no post-delivery buyer sign-off
> to wait on** — do not build logic that blocks on one. The requester's only
> protection is the front-end SLA: fail to deliver in time and the order hits
> `ORDER_EXPIRED` and auto-refunds. So: deliver a valid response = get paid.

Minimal shape (see `examples/provider.py` in the SDK, and `reference.md`):

```python
client = AgentClient(Config(base_url=..., ws_url=...), sdk_key)  # croo_sk_...
stream = await client.connect_websocket()

stream.on(EventType.NEGOTIATION_CREATED, accept_if_valid)   # gate on requirements
stream.on(EventType.ORDER_PAID,          run_pipeline_and_deliver)
stream.on(EventType.ORDER_COMPLETED,     mark_done)
```

The runtime is a long-lived loop — it is the natural home for the **same poll
engine** Catalyst already runs (`catalyst/cli.py:_poll_cycle`). Two integration
shapes, pick per `plan.md`:
1. **On-demand (order-driven):** run the pipeline *when an order is paid*, deliver
   that order's fresh result. Lowest latency to revenue, pay-per-call.
2. **Standing (subscription-like):** keep polling on an interval; when an order
   arrives, deliver the latest computed signals. Better when buyers want a feed.

## Designing the service (the contract with buyers)

Decide these before registering in the Dashboard — they define the product:

- **Requirements schema** (what the buyer sends): keep it a typed JSON object,
  e.g. `{ "assets": ["BTC","ETH"], "signal": "alert|watch", "direction":
  "bullish|bearish|neutral", "horizon": "intraday|short", "min_confidence": 0.5 }`.
  Use `requirements_type=schema` so the Dashboard renders a form and you receive
  structured input on the `Negotiation.requirements` field.
- **Deliverable: use `DeliverableType.SCHEMA`**, not TEXT. Catalyst emits the
  `catalyst.signals` (v2.0) payload — each item is a **watch signal**
  (`signal`: alert|watch, `direction`: bullish|bearish|neutral, confidence,
  horizon, catalysts, layers, rationale), **never** a buy/sell/hold instruction.
  Deliver it as JSON conforming to the declared schema so buyers (other agents)
  consume it machine-readably. Full shape in `reference.md`.
- **Pricing:** flat USDC per call (standard model). The fund-transfer / escrow
  pricing model is for services that *move principal* (swaps, cross-chain) — only
  relevant if Catalyst ever ships the gated Phase 6 execution layer, not for
  selling signals.
- **SLA (`sla_hours`/`sla_minutes`):** must exceed your worst-case pipeline run
  time, or orders auto-refund on timeout and reputation suffers. A poll cycle's
  duration (tracked by the Phase 4 monitoring layer) is your input here.

## Accept / reject gating

`accept_negotiation()` mints an on-chain order — it costs gas and commits to an
SLA. Gate it. Reject (`reject_negotiation`) when: requirements are unparseable,
the requested asset universe is one we don't cover, or monitoring says the
pipeline is unhealthy/stale (don't sell a signal you can't compute in time).
This reuses the Phase 4 health surface directly.

## Where this lands in the project plan

This skill is the platform layer that the rest of `plan.md` must target:

- **Phase 3 (Alerts)** generalizes: a Croo **delivery** is just another `Sink` —
  instead of (or in addition to) webhook/Telegram, a sink fulfills a paid order
  via `deliver_order`. Design the alert/delivery payload once, emit it both ways.
- **Phase 4 (Monitoring)** feeds the accept/reject gate and SLA sizing.
- **Phase 5 (this — Croo provider)** lands the Croo runtime as a new module
  (`catalyst/croo_agent.py`), keeping the pipeline modules intact.
- **Phase 6 (Execution)** is the only place the fund-transfer pricing model and
  `accept_negotiation_with_fund_address` come into play — and it stays gated.

## Build order (do not skip the dry steps)

1. Read `reference.md` (full SDK surface, env vars, fund-transfer caveats).
2. Stand up a **no-op provider** against the SDK: connect, accept, deliver a
   hardcoded JSON. Prove the event loop and auth (`CROO_SDK_KEY`) work end-to-end
   on testnet/sandbox before wiring the pipeline.
3. Define the **requirements + deliverable schema**; register the service in the
   Dashboard; fund the agent **AA wallet** (not the controller) with USDC.
4. Replace the hardcoded delivery with a real pipeline run; map `Action[]` → the
   deliverable schema.
5. Add the accept/reject gate from the monitoring layer.
6. Only then consider standing/subscription mode and the gated Phase 6 (execution).

## Gotchas (verified against the SDK)

- **AA wallet vs controller:** fund the **agent AA wallet** address (in the
  Dashboard), not the controller. `pay_order` pre-checks ERC-20 balance and fails
  fast otherwise — but *that's the requester side*; as a provider you need USDC
  for gas/fees, and any fund-transfer flows pay into `provider_fund_address`.
- **The SDK is fully async** (`httpx` + `websockets`). Catalyst's pipeline is
  sync — run it in a thread (`asyncio.to_thread`) inside the `ORDER_PAID` handler
  so you don't block the event loop / WS heartbeat.
- **Keys are camelCase on the wire, snake_case in dataclasses** — the SDK maps
  both ways automatically (`types.py` `_CAMEL_TO_SNAKE`); use the dataclasses.
- **WebSocket auto-reconnects** (backoff 1s→30s, 30s ping). Don't hand-roll
  reconnect; do make handlers idempotent — a redelivered event must not double-run
  the pipeline or double-deliver an order.
- **Errors are `APIError`** with helpers (`is_not_found`, `is_insufficient_balance`,
  `is_invalid_status`, …). Use them to drive accept/reject and retry logic.
```
