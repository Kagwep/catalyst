#!/usr/bin/env python
"""Croo smoke test — the BUYER half.

Pair this with the provider running in no-op mode:

    catalyst croo-provider --no-op          # terminal 1 (the seller = your agent)
    python scripts/croo_smoke_requester.py  # terminal 2 (this = a throwaway buyer)

It drives the full requester lifecycle against the REAL backend (there is no
testnet — Base mainnet only; gas is platform-sponsored, so the only cost is the
service price, set that to a few cents):

    negotiate_order ─► [provider accepts] ─► pay_order ─► [provider delivers] ─► get_delivery

Then it prints the delivered payload so you can eyeball that the round-trip —
auth, WS/HTTP, on-chain accept, pay, deliver, settle — actually works before you
wire the real pipeline.

Single-agent by default: `CROO_REQUESTER_SDK_KEY` falls back to `CROO_SDK_KEY`, so
you can try buying your OWN service with one agent. This opens no WebSocket (HTTP
only), so it won't trip the duplicate-key WS limit the provider connection holds.
The unknown is whether the backend rejects requester==provider — if it does
(a "requester and provider must differ"-style APIError), create a second agent,
fund its AA wallet, and set CROO_REQUESTER_SDK_KEY to that agent's key. Env:

    CROO_API_URL           https://api.croo.network
    CROO_WS_URL            wss://api.croo.network/ws     (unused here, kept for symmetry)
    CROO_SDK_KEY           croo_sk_...   the provider agent's key (buyer falls back to this)
    CROO_REQUESTER_SDK_KEY croo_sk_...   OPTIONAL — a separate buyer agent, only if self-order is blocked
    CROO_SERVICE_ID        the service_id you registered for the provider
    BASE_RPC_URL           optional; ERC-20 balance pre-check RPC (default Base mainnet)

Override service/requirements via CLI: see --help.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"missing required env var {name} (see the module docstring)")
    return val


async def _poll(fetch, done, *, timeout: float, interval: float, desc: str):
    """Call async `fetch()` every `interval`s until `done(result)`; return result.

    Exits the script on timeout — a smoke test should fail loudly, not hang."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await fetch()
        if done(last):
            return last
        await asyncio.sleep(interval)
    sys.exit(f"timed out after {timeout:.0f}s waiting for {desc} "
             f"(last status={getattr(last, 'status', '?')!r})")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Croo smoke test — buyer side")
    ap.add_argument("--service-id", default=os.environ.get("CROO_SERVICE_ID"),
                    help="service to order (default: $CROO_SERVICE_ID)")
    ap.add_argument("--requirements", default='{"assets": ["BTC"]}',
                    help='JSON requirements sent to the provider (default: %(default)s)')
    ap.add_argument("--timeout", type=float, default=180.0,
                    help="per-stage poll timeout in seconds (default: %(default)s)")
    ap.add_argument("--interval", type=float, default=3.0, help="poll interval seconds")
    ap.add_argument("--negotiate-only", action="store_true",
                    help="probe: just negotiate and stop (moves no funds, no provider needed). "
                         "Use this to check whether the backend lets you request your own agent.")
    args = ap.parse_args()

    service_id = args.service_id or sys.exit("missing --service-id / $CROO_SERVICE_ID")
    # Validate the requirements are JSON before we spend anything.
    json.loads(args.requirements)

    from croo import (
        AgentClient, Config, ListOptions,
        NegotiateOrderRequest, NegotiationStatus, OrderStatus, APIError,
    )

    # Buyer key: a dedicated requester agent if given, else fall back to the
    # provider's own key (single-agent self-order — try this first).
    buyer_key = os.environ.get("CROO_REQUESTER_SDK_KEY") or os.environ.get("CROO_SDK_KEY")
    if not buyer_key:
        sys.exit("set CROO_REQUESTER_SDK_KEY (a separate buyer agent) or CROO_SDK_KEY")
    if not os.environ.get("CROO_REQUESTER_SDK_KEY"):
        print("note: no CROO_REQUESTER_SDK_KEY — buying with the provider's own key "
              "(single-agent self-order). If the backend rejects it, use a 2nd agent.")

    client = AgentClient(
        Config(
            base_url=_env("CROO_API_URL"),
            ws_url=os.environ.get("CROO_WS_URL", ""),
            rpc_url=os.environ.get("BASE_RPC_URL", ""),
        ),
        buyer_key,
    )

    try:
        # 1. Negotiate — asks the provider for an order against our requirements.
        # This is the self-order permission gate: if the backend forbids
        # requester==provider it errors HERE, before any funds move.
        print(f"[1/5] negotiating service={service_id} requirements={args.requirements}")
        try:
            neg = await client.negotiate_order(NegotiateOrderRequest(
                service_id=service_id, requirements=args.requirements,
            ))
        except APIError as e:
            print(f"      negotiate rejected: {e}")
            sys.exit("❌ could not request this service. If this is a self-order "
                     "(buyer key == provider key), the backend likely forbids "
                     "requester==provider — create a 2nd agent and set "
                     "CROO_REQUESTER_SDK_KEY. Otherwise check the service_id.")
        nid = neg.negotiation_id
        print(f"      negotiation_id={nid} status={neg.status}")
        if args.negotiate_only:
            print("\n✅ negotiate accepted — you CAN request this agent. "
                  "(probe only; no order paid). Re-run without --negotiate-only for the "
                  "full round-trip once the provider is running and the wallet is funded.")
            return

        # 2. Wait for the provider's accept/reject decision.
        print("[2/5] waiting for provider to accept ...")
        neg = await _poll(
            lambda: client.get_negotiation(nid),
            lambda n: n.status in (NegotiationStatus.ACCEPTED, NegotiationStatus.REJECTED,
                                   NegotiationStatus.EXPIRED),
            timeout=args.timeout, interval=args.interval, desc="negotiation decision",
        )
        if neg.status != NegotiationStatus.ACCEPTED:
            sys.exit(f"provider did not accept: status={neg.status} "
                     f"reason={neg.reject_reason!r}")
        print(f"      accepted")

        # Accept mints the on-chain order; find it by negotiation_id (requester role).
        orders = await client.list_orders(ListOptions(role="requester", page_size=50))
        match = [o for o in orders if o.negotiation_id == nid]
        if not match:
            sys.exit("accepted but no order found for this negotiation "
                     "(try again / check list_orders paging)")
        order = match[0]
        print(f"      order_id={order.order_id} price={order.price} "
              f"token={order.payment_token} status={order.status}")

        # 3. Pay — pre-checks our ERC-20 balance, then submits the pay tx.
        print("[3/5] paying order ...")
        try:
            pay = await client.pay_order(order.order_id)
        except APIError as e:
            sys.exit(f"pay failed: {e} (is the buyer AA wallet funded with USDC?)")
        print(f"      paid tx={pay.tx_hash} status={pay.order.status}")

        # 4. Wait for the provider to deliver → order completes and escrow settles.
        print("[4/5] waiting for delivery / completion ...")
        order = await _poll(
            lambda: client.get_order(order.order_id),
            lambda o: o.status in (OrderStatus.COMPLETED, OrderStatus.REJECTED,
                                   OrderStatus.EXPIRED, OrderStatus.DELIVER_FAILED),
            timeout=args.timeout, interval=args.interval, desc="order completion",
        )
        if order.status != OrderStatus.COMPLETED:
            sys.exit(f"order did not complete: status={order.status}")
        print(f"      completed deliver_tx={order.deliver_tx_hash}")

        # 5. Fetch the deliverable and print it — proof the payload round-tripped.
        print("[5/5] fetching delivery ...")
        delivery = await client.get_delivery(order.order_id)
        body = delivery.deliverable_schema or delivery.deliverable_text
        print(f"      delivery_id={delivery.delivery_id} type={delivery.deliverable_type} "
              f"content_hash={delivery.content_hash}")
        try:
            print(json.dumps(json.loads(body), indent=2))
        except (json.JSONDecodeError, TypeError):
            print(body)
        print("\n✅ smoke test passed — negotiate → pay → deliver → fetch all worked.")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
