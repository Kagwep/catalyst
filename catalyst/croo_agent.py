"""Croo provider — sell Catalyst's proposals as a service on the Croo Network.

The platform layer the rest of the build targeted. Croo is the storefront +
checkout + delivery rail; the pipeline is the product. This module is the async
runtime that listens for orders and fulfils them:

    NEGOTIATION_CREATED ─► gate (health + coverage) ─► accept / reject
    ORDER_PAID          ─► run the pipeline off-loop ─► deliver_order(SCHEMA)

It reuses everything already built: the deliverable is the canonical
`payload.build_payload` (same bytes an alert webhook sends — a Croo delivery is
"just another sink"); the accept/reject gate reads the Phase-4 health surface;
the buyer's `requirements` narrow the result via `payload.select_actions`.

Design constraints honoured (see the `croo-provider` skill):
  - **SDK is runtime-only + not a hard dep.** All `croo` imports are lazy, so the
    package imports and the unit tests run without the SDK installed; the mock
    client drives the handlers directly.
  - **Never block the WS read loop.** The sync pipeline runs in `asyncio.to_thread`.
  - **Idempotent delivery.** A redelivered `ORDER_PAID` (reconnect) is guarded by a
    local delivered-set *and* the on-chain order status — never double-deliver.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

logger = logging.getLogger("catalyst.croo")

# Order statuses that mean this order is already past "paid" — a redelivered
# ORDER_PAID for one of these must be a no-op (wire values from croo.OrderStatus).
_ALREADY_HANDLED = frozenset({
    "delivering", "completed", "rejecting", "rejected", "expired", "deliver_failed",
})


def parse_requirements(raw: str | None):
    """Parse a Negotiation.requirements JSON string.

    Returns a dict on success, `{}` for empty/absent (no filter — valid), or
    `None` when the string is present but not a JSON object (→ reject)."""
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return val if isinstance(val, dict) else None


# ---------------------------------------------------------------------------
# Default pipeline + health (both injectable for tests)
# ---------------------------------------------------------------------------


def default_pipeline(
    db_path: str, requirements: dict, *, weights: dict | None = None, present=None,
) -> dict:
    """Compute fresh proposals from the store, filter to the buyer's requirements,
    and return the canonical deliverable payload.

    Order-driven: reads the already-ingested/enriched store and runs the real
    signal→bias→planner path (no network ingest in the paid path — that's the poll
    loop's job, so delivery stays inside the SLA). The market/momentum modifier is
    omitted here (it needs a live price fetch); every other layer is applied.

    `present` is an optional grounded narration callable (see present.py): when
    given, its `summary`/`catalyst_notes`/`layer_notes` are merged onto the flat
    payload. It only describes the numbers already computed — never changes them —
    and any failure is swallowed so delivery is never blocked by the LLM."""
    from .derivs import compute_derivs_bias
    from .flows import compute_flow_bias
    from .macro import compute_regime
    from .onchain import compute_supply_bias
    from .payload import (
        DEFAULT_WINDOW_HOURS, build_payload, flatten_signals, requirements_to_kwargs,
        requirements_window_hours, select_actions,
    )
    from .planner import plan
    from .signals import compute_signals
    from .store import (
        fetch_derivs, fetch_enriched, fetch_flows, fetch_macro, fetch_onchain, open_store,
    )
    from .trend import compute_trend_bias

    # Buyer-selectable lookback: anything from an hour up to a week. Absent →
    # the default 24h. Trend keeps its own multi-day window (bias-slope context).
    window_hours = requirements_window_hours(requirements) or DEFAULT_WINDOW_HOURS

    conn = open_store(db_path)
    try:
        enriched = fetch_enriched(conn)
        sigs = compute_signals(enriched, window_hours=window_hours)
        actions = plan(
            sigs,
            regime=compute_regime(fetch_macro(conn)),
            flow_bias=compute_flow_bias(fetch_flows(conn)),
            supply_bias=compute_supply_bias(fetch_onchain(conn)),
            derivs_bias=compute_derivs_bias(fetch_derivs(conn)),
            trend_bias=compute_trend_bias(conn, [s.asset for s in sigs]),
        )
    finally:
        conn.close()

    universe = sorted({a.asset for a in actions})
    selected = select_actions(actions, **requirements_to_kwargs(requirements))
    payload = build_payload(selected, meta={
        "universe": universe, "requirements": requirements, "window_hours": window_hours,
    })
    flat = flatten_signals(payload)   # Dashboard-builder shape (asset-keyed, no deep nesting)

    if present is not None:
        try:
            from .present import catalyst_headlines

            top_asset = (flat.get("actions") or {}).get("asset")
            heads = catalyst_headlines(enriched, top_asset, window_hours)
            flat.update(present(flat, heads))   # adds prose fields only; numbers untouched
        except Exception as err:  # noqa: BLE001 — narration is optional, never block delivery
            logger.warning("presenter failed, delivering without narrative: %s", err)
    return flat


def no_op_pipeline(requirements: dict) -> dict:
    """A hardcoded, canonical-shaped deliverable — the SDK-smoke probe.

    Build-order step 2: prove the event loop + auth + accept/deliver round-trip
    against the real Croo backend *before* trusting the pipeline. It runs no store,
    no signal math, no network — just returns one illustrative proposal in the
    exact `catalyst.signals` schema a real delivery uses (via `build_payload`, so
    the shape can't drift), echoing the buyer's `requirements` into `meta` so you
    can eyeball that structured input reached the handler end-to-end.
    """
    from types import SimpleNamespace

    from .payload import build_payload, flatten_signals

    probe = SimpleNamespace(
        asset="BTC", action="watch", direction="neutral", confidence=0.0,
        horizon="intraday", score=0.0, catalysts=["__no_op_probe__"],
        freshness_minutes=0.0, layers={}, created_at="1970-01-01T00:00:00Z",
        rationale="NO-OP SMOKE TEST — not a real signal. Proves the Croo delivery "
                  "path works; the pipeline is not wired in this mode.",
    )
    return flatten_signals(build_payload(
        [probe], meta={"mode": "no-op", "universe": ["BTC"], "requirements": requirements}))


def default_health(db_path: str) -> tuple[bool, str]:
    """Accept/reject health signal from the Phase-4 monitoring surface."""
    from .monitoring import status_report
    from .store import open_store

    conn = open_store(db_path)
    try:
        rep = status_report(conn)
    finally:
        conn.close()
    if rep["healthy"]:
        return True, "healthy"
    return False, f"{len(rep.get('ops_issues') or [])} ops issue(s), error streak {rep['error_streak']}"


def _default_deliver_factory(payload: dict):
    """Build the SDK's DeliverOrderRequest (lazy import — SDK only needed at run)."""
    from croo import DeliverableType, DeliverOrderRequest

    return DeliverOrderRequest(
        deliverable_type=DeliverableType.SCHEMA,
        deliverable_schema=json.dumps(payload),
    )


def build_client_from_env():
    """Construct a croo.AgentClient from CROO_* env vars (raises if SDK absent)."""
    try:
        from croo import AgentClient, Config
    except ImportError as err:  # pragma: no cover - depends on the optional SDK
        raise RuntimeError(
            "croo SDK not installed — `pip install croo-sdk` (or add D:\\projects\\python-sdk)"
        ) from err
    try:
        base_url = os.environ["CROO_API_URL"]
        ws_url = os.environ["CROO_WS_URL"]
        sdk_key = os.environ["CROO_SDK_KEY"]
    except KeyError as err:  # pragma: no cover - config error
        raise RuntimeError(f"missing required env var {err}") from err
    return AgentClient(Config(base_url=base_url, ws_url=ws_url,
                              rpc_url=os.environ.get("BASE_RPC_URL", "")), sdk_key)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


def make_no_op_provider(client, **kwargs) -> "CrooProvider":
    """A `CrooProvider` wired for the SDK smoke test: accept every negotiation
    (always-healthy gate, no coverage filter) and deliver a static probe payload.

    Only the two decision seams are swapped — the accept/idempotency/deliver
    machinery is the same code the real provider runs, so a green smoke test
    exercises the actual event loop, not a parallel one."""
    kwargs.setdefault("health", lambda: (True, "no-op smoke test"))
    kwargs.setdefault("covered_assets", None)
    return CrooProvider(client, pipeline=no_op_pipeline, **kwargs)


class CrooProvider:
    """The provider event loop over an `AgentClient` (or any object matching it).

    `client` is injected so tests can drive the handlers with a mock. `pipeline`,
    `health`, and `deliver_factory` are injectable seams around the two network-
    free decision points (what to deliver, whether to accept).
    """

    def __init__(
        self, client, *, db_path: str = "catalyst.db", covered_assets=None,
        pipeline=None, health=None, weights: dict | None = None, deliver_factory=None,
        present=None,
    ):
        self.client = client
        self.db_path = db_path
        self.covered_assets = {a.upper() for a in covered_assets} if covered_assets else None
        self._pipeline = pipeline or (
            lambda req: default_pipeline(db_path, req, weights=weights, present=present)
        )
        self._health = health or (lambda: default_health(db_path))
        self._deliver_factory = deliver_factory or _default_deliver_factory
        self._delivered: set[str] = set()      # order_ids we've delivered (idempotency)
        self._stream = None

    # ---- gate ----

    def gate(self, requirements) -> tuple[bool, str]:
        """Decide whether to accept an order: parseable, covered, and healthy."""
        if requirements is None:
            return False, "unparseable requirements"
        want = requirements.get("assets") or requirements.get("asset")
        if isinstance(want, str):   # Dashboard v2 sends assets as a comma-separated string
            want = [s.strip().strip("\"'").strip() for s in want.split(",") if s.strip().strip("\"'").strip()]
        if self.covered_assets is not None and want:
            wanted = {a.upper() for a in want}
            if not (wanted & self.covered_assets):
                return False, f"unsupported assets: {sorted(wanted)}"
        ok, reason = self._health()
        if not ok:
            return False, f"pipeline unhealthy: {reason}"
        return True, "ok"

    # ---- async handlers (directly unit-testable) ----

    async def handle_negotiation(self, negotiation_id: str) -> tuple[str, str]:
        neg = await self.client.get_negotiation(negotiation_id)
        req = parse_requirements(getattr(neg, "requirements", ""))
        ok, reason = self.gate(req)
        if not ok:
            await self.client.reject_negotiation(negotiation_id, reason)
            logger.info("rejected negotiation %s: %s", negotiation_id, reason)
            return "rejected", reason
        await self.client.accept_negotiation(negotiation_id)
        logger.info("accepted negotiation %s", negotiation_id)
        return "accepted", "ok"

    async def handle_paid(self, order_id: str) -> tuple[str, str]:
        if order_id in self._delivered:
            return "skipped", "already delivered"
        order = await self.client.get_order(order_id)
        status = getattr(order, "status", "")
        if status in _ALREADY_HANDLED:
            self._delivered.add(order_id)      # remember so we don't re-fetch next time
            return "skipped", f"order status {status}"

        neg = await self.client.get_negotiation(order.negotiation_id)
        req = parse_requirements(getattr(neg, "requirements", "")) or {}
        # Sync pipeline off the event loop so the WS heartbeat keeps ticking.
        payload = await asyncio.to_thread(self._pipeline, req)
        await self.client.deliver_order(order_id, self._deliver_factory(payload))
        self._delivered.add(order_id)
        logger.info("delivered order %s (%d actions)", order_id, payload.get("count", 0))
        return "delivered", "ok"

    # ---- sync WS callbacks (schedule the async work; never block the read loop) ----

    def _on_negotiation(self, event):
        asyncio.create_task(self._guard(self.handle_negotiation(event.negotiation_id), event))

    def _on_paid(self, event):
        asyncio.create_task(self._guard(self.handle_paid(event.order_id), event))

    async def _guard(self, coro, event):
        try:
            await coro
        except Exception as err:  # noqa: BLE001 — one bad event must not kill the stream
            logger.exception("croo handler failed (%s): %s", getattr(event, "type", "?"), err)

    async def start(self):
        """Connect the single EventStream, register the handlers, return the stream.

        Split out of `run()` (which just idles forever after this) so the whole
        loop — connect → dispatch → accept/deliver — is drivable end-to-end in a
        test against a fake stream, with no network and no infinite wait."""
        from croo import EventType

        self._stream = await self.client.connect_websocket()
        self._stream.on(EventType.NEGOTIATION_CREATED, self._on_negotiation)
        self._stream.on(EventType.ORDER_PAID, self._on_paid)
        logger.info("croo provider listening (db=%s, coverage=%s)",
                    self.db_path, sorted(self.covered_assets) if self.covered_assets else "all")
        return self._stream

    async def run(self) -> None:
        """Connect and register handlers, then idle until cancelled."""
        await self.start()
        await asyncio.Event().wait()
