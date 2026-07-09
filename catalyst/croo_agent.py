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
    from .signals import compute_signals, signal_kwargs_from_weights
    from .store import (
        fetch_derivs, fetch_enriched, fetch_flows, fetch_macro, fetch_onchain, open_store,
    )
    from .trend import compute_trend_bias

    # Buyer-selectable lookback: anything from an hour up to a week. Absent →
    # the default 24h. Trend keeps its own multi-day window (bias-slope context).
    window_hours = requirements_window_hours(requirements) or DEFAULT_WINDOW_HOURS

    # Honour a tuned weights artifact end-to-end: the same Phase-8 knobs the CLI
    # applies (signal weights + planner modifier weights, buy_threshold, and the
    # confidence calibration table) so paid delivery uses the fitted scorer, not
    # the raw defaults. Absent `weights` → every override is empty → unchanged.
    weights = weights or {}
    signal_kwargs = signal_kwargs_from_weights(weights)
    plan_kwargs = dict(weights.get("modifier_weights") or {})
    if weights.get("buy_threshold") is not None:
        plan_kwargs["buy_threshold"] = weights["buy_threshold"]
    if weights.get("confidence_calibration"):
        plan_kwargs["confidence_calibration"] = weights["confidence_calibration"]

    conn = open_store(db_path)
    try:
        enriched = fetch_enriched(conn)
        sigs = compute_signals(enriched, window_hours=window_hours, **signal_kwargs)
        actions = plan(
            sigs,
            regime=compute_regime(fetch_macro(conn)),
            flow_bias=compute_flow_bias(fetch_flows(conn)),
            supply_bias=compute_supply_bias(fetch_onchain(conn)),
            derivs_bias=compute_derivs_bias(fetch_derivs(conn)),
            trend_bias=compute_trend_bias(conn, [s.asset for s in sigs]),
            **plan_kwargs,
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


_SEV_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}


def _age_str(dt, now) -> str:
    mins = max(0.0, (now - dt).total_seconds() / 60.0)
    if mins < 60:
        return f"{int(mins)}m ago"
    if mins < 1440:
        return f"{int(mins // 60)}h ago"
    return f"{int(mins // 1440)}d ago"


def events_pipeline(db_path: str, requirements: dict) -> dict:
    """The `catalyst.events` service: a breadth feed of market-moving catalyst
    events, read straight from the enriched store (the `event`/`severity` written
    at enrich time). No LLM at serve time.

    Buyer filters (all optional strings): `assets`, `catalysts`, `min_severity`
    (default medium), `direction`, `window` (default 24h), `limit` (default 20)."""
    from datetime import datetime, timezone, timedelta

    from .payload import (
        DEFAULT_WINDOW_HOURS, build_events_delivery, requirements_window_hours,
    )
    from .signals import _assets, _parse_dt
    from .store import fetch_events, open_store

    req = requirements or {}

    def _csv(v):
        if not v:
            return None
        vals = [s.strip().strip("\"'").strip() for s in str(v).split(",")]
        return {s for s in vals if s} or None

    window_hours = requirements_window_hours(req) or DEFAULT_WINDOW_HOURS
    want_assets = {a.upper() for a in (_csv(req.get("assets")) or set())} or None
    want_cats = {c.lower() for c in (_csv(req.get("catalysts")) or set())} or None
    want_dir = (str(req.get("direction")).strip().lower() or None) if req.get("direction") else None
    min_sev = str(req.get("min_severity") or "medium").strip().lower()
    min_rank = _SEV_RANK.get(min_sev, 2)
    try:
        limit = max(1, min(50, int(str(req.get("limit") or 20).strip())))
    except (TypeError, ValueError):
        limit = 20

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)

    conn = open_store(db_path)
    try:
        rows = fetch_events(conn)        # news-source posts with a concrete event
    finally:
        conn.close()

    picked: list[tuple] = []
    for r in rows:
        ev = r.get("event")
        if not ev:                        # only posts with a concrete event
            continue
        sev = (r.get("severity") or "none").lower()
        if _SEV_RANK.get(sev, 0) < min_rank:
            continue
        dt = _parse_dt(r.get("indexed_at"))
        if dt is None or dt < cutoff:
            continue
        assets = _assets(r)
        asset = next((a for a in assets if not want_assets or a.upper() in want_assets),
                     None if want_assets else (assets[0] if assets else "MARKET"))
        if asset is None:                 # buyer asked for assets none of which match
            continue
        cat = (r.get("catalyst") or "").lower()
        if want_cats and cat not in want_cats:
            continue
        s = r.get("sentiment_score") or 0.0
        direction = "bullish" if s > 0.1 else "bearish" if s < -0.1 else "neutral"
        if want_dir and direction != want_dir:
            continue
        picked.append((_SEV_RANK.get(sev, 0), dt, {
            "asset": asset.upper(), "catalyst": cat or None, "event": ev,
            "direction": direction, "severity": sev, "sentiment": round(float(s), 3),
            "source": r.get("source"), "url": r.get("url"),
            "at": dt.isoformat(), "age": _age_str(dt, now),
        }))

    # Most market-moving first, then most recent.
    picked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    events = [e for _, _, e in picked[:limit]]
    return build_events_delivery(events, meta={
        "window_hours": window_hours, "requirements": req,
    })


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
        present=None, services=None,
    ):
        self.client = client
        self.db_path = db_path
        self.covered_assets = {a.upper() for a in covered_assets} if covered_assets else None
        self._pipeline = pipeline or (
            lambda req: default_pipeline(db_path, req, weights=weights, present=present)
        )
        # Extra services keyed by their Croo service_id (e.g. the events feed). An
        # order whose service_id isn't here falls through to the default signal
        # pipeline, so a single-service provider behaves exactly as before.
        self._services = dict(services or {})
        self._health = health or (lambda: default_health(db_path))
        self._deliver_factory = deliver_factory or _default_deliver_factory
        self._delivered: set[str] = set()      # order_ids we've delivered (idempotency)
        self._stream = None

    def _pipeline_for(self, service_id):
        return self._services.get(service_id, self._pipeline)

    # ---- gate ----

    def gate(self, requirements, *, check_coverage: bool = True) -> tuple[bool, str]:
        """Decide whether to accept an order: parseable, covered, and healthy.

        `check_coverage=False` skips the asset-coverage filter — used for the
        events feed, which serves the whole market rather than a fixed universe."""
        if requirements is None:
            return False, "unparseable requirements"
        if check_coverage and self.covered_assets is not None:
            want = requirements.get("assets") or requirements.get("asset")
            if isinstance(want, str):   # Dashboard v2 sends assets as a comma-separated string
                want = [s.strip().strip("\"'").strip() for s in want.split(",") if s.strip().strip("\"'").strip()]
            if want:
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
        # Coverage only constrains the signal service; extra services (events)
        # serve the whole market, so skip the asset-coverage check for them.
        is_extra = getattr(neg, "service_id", None) in self._services
        ok, reason = self.gate(req, check_coverage=not is_extra)
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
        # Route to the pipeline for this order's service (events vs signals).
        pipeline = self._pipeline_for(getattr(order, "service_id", None))
        # Sync pipeline off the event loop so the WS heartbeat keeps ticking.
        payload = await asyncio.to_thread(pipeline, req)
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
