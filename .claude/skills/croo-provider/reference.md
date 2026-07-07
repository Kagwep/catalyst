# Croo SDK Reference (for the Catalyst provider)

Distilled from the local SDK at `D:\projects\python-sdk` (package `croo`,
v0.2.1) and `https://docs.croo.network/`. The SDK is **runtime only** — account,
service registration, and SDK-Key issuance happen in the Croo Dashboard.

## Install & import

```bash
pip install croo-sdk      # Python 3.10+, deps: httpx>=0.27, websockets>=13
```

```python
from croo import (
    AgentClient, Config, EventStream,
    EventType, Event,
    DeliverableType, DeliverOrderRequest, DeliverOrderResult, Delivery, DeliveryStatus,
    NegotiateOrderRequest, Negotiation, NegotiationStatus, AcceptNegotiationResult,
    Order, OrderStatus, PayOrderResult, ListOptions,
    APIError, InsufficientBalanceError,
    is_not_found, is_unauthorized, is_invalid_params,
    is_invalid_status, is_forbidden, is_insufficient_balance,
)
```

## Configuration & env

```python
client = AgentClient(
    Config(base_url=..., ws_url=..., rpc_url=...),  # rpc_url optional → Base mainnet
    sdk_key,                                         # "croo_sk_..."
)
```

| Env var | Meaning |
|---|---|
| `CROO_API_URL` | REST base, e.g. `https://api.croo.network` |
| `CROO_WS_URL` | WebSocket, e.g. `wss://api.croo.network/ws` |
| `CROO_SDK_KEY` | SDK key, `croo_sk_...` (sent as `X-SDK-Key`; WS sends it as `?key=`) |
| `BASE_RPC_URL` | optional ERC-20 balance-check RPC; default `https://mainnet.base.org` |

> **No testnet/sandbox — Base Mainnet only (Chain ID 8453), real USDC**
> (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`). The SDK has no environment
> switch; the only documented endpoints are prod (`api.croo.network`). **Gas is
> platform-sponsored**, so you only need real USDC equal to the order *price*, not
> ETH. Cheapest test = register a service priced at a few cents and self-order from
> a second funded agent; the price round-trips back minus the platform fee.

## AgentClient methods (all `async`)

**Negotiation**
- `negotiate_order(NegotiateOrderRequest) -> Negotiation` *(requester side)*
- `accept_negotiation(negotiation_id) -> AcceptNegotiationResult` — **provider**; mints the on-chain order
- `accept_negotiation_with_fund_address(negotiation_id, provider_fund_address) -> AcceptNegotiationResult` — **only** for services with `require_fund_transfer=true`
- `reject_negotiation(negotiation_id, reason) -> None`
- `get_negotiation(negotiation_id) -> Negotiation`
- `list_negotiations(ListOptions?) -> list[Negotiation]`

**Order lifecycle**
- `get_order(order_id) -> Order`
- `list_orders(ListOptions?) -> list[Order]`
- `pay_order(order_id) -> PayOrderResult` *(requester; pre-checks ERC-20 balance)*
- `deliver_order(order_id, DeliverOrderRequest) -> DeliverOrderResult` — **provider**
- `reject_order(order_id, reason) -> None`
- `get_delivery(order_id) -> Delivery`

**Object storage** (for large/binary deliverables)
- `upload_file(file_name, bytes|BinaryIO) -> object_key` *(presigned PUT)*
- `get_download_url(object_key) -> url` *(valid ~30 min)*

**WebSocket**
- `connect_websocket() -> EventStream`
- `close()` — close the HTTP client

## EventStream

```python
stream = await client.connect_websocket()
stream.on(EventType.ORDER_PAID, handler)     # handler(e: Event) -> None  (SYNC callable)
stream.on_any(handler)
await stream.close()
```

- Auto-reconnect: exponential backoff `2**attempt` capped at 30s; 30s ping / 60s pong timeout. **Do not hand-roll reconnect.**
- **Duplicate SDK-Key** connections get a policy-violation close (1008) and will **not** reconnect — run exactly one EventStream per key.
- **Handlers are invoked synchronously** inside the read loop. A blocking handler stalls the socket. Schedule work: `asyncio.create_task(...)`, and run sync/CPU work via `asyncio.to_thread(...)`.

### EventType → trigger

| `EventType` | wire value | When |
|---|---|---|
| `NEGOTIATION_CREATED` | `order_negotiation_created` | new negotiation (provider: decide accept/reject) |
| `NEGOTIATION_REJECTED` | `order_negotiation_rejected` | negotiation rejected |
| `NEGOTIATION_EXPIRED` | `order_negotiation_expired` | negotiation expired |
| `ORDER_CREATED` | `order_created` | on-chain order minted (requester: pay) |
| `ORDER_PAID` | `order_paid` | payment confirmed (**provider: do work + deliver**) |
| `ORDER_COMPLETED` | `order_completed` | escrow settled to provider (auto on delivery — **not** a buyer acceptance gate) |
| `ORDER_REJECTED` | `order_rejected` | order rejected |
| `ORDER_EXPIRED` | `order_expired` | SLA breach (auto-refund) |

`Event` fields: `type, raw, negotiation_id, order_id, requester_agent_id, provider_agent_id, service_id, status, reason`.

## Key dataclasses (selected fields)

**Negotiation** — `negotiation_id, service_id, requester_agent_id, provider_agent_id, requirements, status, reject_reason, metadata, expires_at`, + fund-transfer: `fund_amount, fund_token, provider_fund_address`. `requirements` is the JSON the buyer submitted against your requirements schema.

**Order** — `order_id, negotiation_id, chain_order_id, service_id, price, payment_token, delivery_window, status, sla_deadline, pay_deadline`, tx hashes (`create/pay/deliver/reject/clear_tx_hash`), timestamps, + fund-transfer: `fee_amount, fund_amount, fund_token, provider_fund_address`.

**DeliverOrderRequest** — `deliverable_type` (`DeliverableType.TEXT|SCHEMA`), `deliverable_schema`, `deliverable_text`.

**Delivery** — `delivery_id, order_id, deliverable_type, deliverable_schema, deliverable_text, content_hash, status` (`submitted|accepted|rejected`), `submitted_at, verified_at`. **Note:** the `accepted`/`rejected` statuses and `verified_at` exist in the type system, but per the live docs settlement is **automatic on delivery** — there is no buyer-driven acceptance step that gates payout. Only the deliverable's keccak256 `content_hash` is written on-chain; the content itself is stored off-chain and fetched by the buyer via `get_delivery` / `get_download_url`. Don't build a provider flow that waits for the buyer to "accept".

`OrderStatus`: `creating, created, paying, paid, delivering, completed, rejecting, rejected, expired, create_failed, pay_failed, deliver_failed`.

## Errors

All API failures raise `APIError` (`e.code`, `e.reason`, `str(e)`). Predicate helpers: `is_not_found, is_unauthorized, is_invalid_params, is_invalid_status, is_forbidden, is_insufficient_balance`. Use these to drive accept/reject and retry.

## Service registration (Dashboard — reference, not SDK)

Configured via Agent Store "+ Add Service" wizard:
- `name`, `description`, `price` (USDC/call), `sla_hours`/`sla_minutes`
- `deliverable_type`: `text` | `schema`  → **use `schema`** for Catalyst
- `requirements_type`: `none` | `text` | `schema` → **use `schema`** (typed buyer input)
- **Require Fund Transfer** toggle → fund-transfer pricing (swaps/cross-chain). **Leave OFF** for signal-selling; only relevant to gated Phase 6 execution.
- Services live off-chain in the CROO Data Center and are discoverable by `service_id`.

## Catalyst mapping (the deliverable schema)

The deliverable is `catalyst.signals` (v2.0), produced by `payload.build_payload`
and delivered as a `SCHEMA` JSON blob. It is a **watch signal, never a trade
instruction**: the planner reasons internally in buy/sell/watch, but the payload
exposes only a non-prescriptive `signal` (`alert` | `watch`) plus a market
`direction` (`bullish` | `bearish` | `neutral`). A "buy" surfaces as a bullish
alert, a "sell" as a bearish alert. **No buy/sell/hold verb ever leaves the
oracle** (`payload.signal_of` enforces this). Exact shape:

```json
{
  "schema": "catalyst.signals",
  "version": "2.0",
  "generated_at": "2026-07-01T12:00:00Z",
  "disclaimer": "Proposals only — not financial advice…",
  "count": 1,
  "actions": [
    {
      "asset": "ARB",
      "signal": "alert",          // alert | watch  (never buy/sell/hold)
      "direction": "bullish",     // bullish | bearish | neutral
      "confidence": 0.71,
      "score": 0.53,
      "horizon": "intraday",      // intraday (hours) | short (days) | swing (multi-day, trend-driven)
      "catalysts": ["upgrade"],
      "freshness_minutes": 12.0,
      "layers": { "macro": {"label": "risk-on", "bias": 0.1, "effect": "boost", "weight": 0.3} },
      "rationale": "BULLISH ALERT ARB | score +0.53 | …",
      "created_at": "2026-07-01T11:48:00Z"
    }
  ],
  "meta": { "universe": ["BTC", "ETH"], "requirements": { "assets": ["ARB"] } }
}
```

Fields map from `catalyst/planner.py::Action` via `action_to_dict`, except
`action`→`signal` (through `signal_of`). Keep the disclaimer — the oracle
surfaces catalyst-driven signals, it never sizes, places, or manages trades.

**Requirements schema (buyer input, `requirements_type=schema`)** — narrows the
result via `payload.select_actions`; all optional, AND-combined:

```json
{ "assets": ["BTC","ETH"], "signal": "alert", "direction": "bullish",
  "horizon": "intraday", "min_confidence": 0.5 }
```

Keys accept a scalar or a list (`signal`/`signals`, `direction`/`directions`,
`horizon`/`horizons`). Filters are on the watch vocabulary — **not** buy/sell.
The `horizon` enum is **`intraday` (hours) | `short` (days) | `swing` (multi-day,
set when the trend layer sees a persistent multi-day move)**.

> **Dashboard v2 can't register an array-of-strings requirements field** ("not
> yet supported in v2"). So register **`assets` as a plain string** and have
> buyers send a **comma-separated** list, e.g. `"BTC,ETH"` (or a single `"DOGE"`).
> `requirements_to_kwargs._list` splits it back into a ticker list, and also
> accepts the singular key `asset`. (The *deliverable* schema-builder does support
> array-of-strings — that's why `catalysts`/`universe` register fine as arrays.)

## Provider skeleton

```python
import asyncio, os, json
from croo import AgentClient, Config, EventType, DeliverableType, DeliverOrderRequest

client = AgentClient(Config(
    base_url=os.environ["CROO_API_URL"],
    ws_url=os.environ["CROO_WS_URL"],
    rpc_url=os.environ.get("BASE_RPC_URL", ""),
), os.environ["CROO_SDK_KEY"])

async def main():
    stream = await client.connect_websocket()

    def on_negotiation(e):
        async def go():
            neg = await client.get_negotiation(e.negotiation_id)
            req = json.loads(neg.requirements or "{}")
            if not healthy() or not covered(req):           # Phase 4 gate
                await client.reject_negotiation(e.negotiation_id, "out of scope / unhealthy")
                return
            await client.accept_negotiation(e.negotiation_id)
        asyncio.create_task(go())

    def on_paid(e):
        async def go():
            order = await client.get_order(e.order_id)
            req = json.loads((await client.get_negotiation(order.negotiation_id)).requirements or "{}")
            result = await asyncio.to_thread(run_catalyst_pipeline, req)   # sync pipeline off the loop
            await client.deliver_order(e.order_id, DeliverOrderRequest(
                deliverable_type=DeliverableType.SCHEMA,
                deliverable_schema=json.dumps(result),
            ))
        asyncio.create_task(go())

    stream.on(EventType.NEGOTIATION_CREATED, on_negotiation)
    stream.on(EventType.ORDER_PAID, on_paid)

    await asyncio.Event().wait()

asyncio.run(main())
```

Make `on_paid` **idempotent** (guard on `order.status`/a delivered set) — a
reconnect can redeliver an event, and you must not double-run or double-deliver.
