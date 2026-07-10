"""Croo provider — accept/reject gating, delivery, idempotency (SDK fully mocked).

No network and no `croo` install: a FakeClient stands in for AgentClient and the
async handlers are driven directly with asyncio.run.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from catalyst.croo_agent import (
    CrooProvider, default_pipeline, make_no_op_provider, no_op_pipeline, parse_requirements,
)
from catalyst.models import Author, Metrics, Post
from catalyst.store import open_store, save_enrichments, save_posts


class FakeClient:
    """Mimics the AgentClient surface the provider uses."""

    def __init__(self, *, negotiation=None, order=None):
        self._neg = negotiation
        self._order = order
        self.accepted: list = []
        self.rejected: list = []
        self.delivered: list = []

    async def get_negotiation(self, nid):
        return self._neg

    async def get_order(self, oid):
        return self._order

    async def accept_negotiation(self, nid):
        self.accepted.append(nid)

    async def reject_negotiation(self, nid, reason):
        self.rejected.append((nid, reason))

    async def deliver_order(self, oid, request):
        self.delivered.append((oid, request))


def _neg(requirements="", nid="neg1"):
    return SimpleNamespace(negotiation_id=nid, requirements=requirements)


def _order(oid="ord1", status="paid", nid="neg1"):
    return SimpleNamespace(order_id=oid, status=status, negotiation_id=nid)


# capture the delivered payload without needing the croo SDK's dataclass
def _capture_factory(payload):
    return {"schema": payload}


# ---- requirements parsing ---------------------------------------------------

def test_provider_routes_by_service_id():
    """An order for the events service_id runs the events pipeline; any other
    service_id (or none) falls through to the default signal pipeline."""
    sig = lambda req: {"schema": "catalyst.signals"}     # noqa: E731
    evt = lambda req: {"schema": "catalyst.events"}      # noqa: E731

    def deliver_for(service_id):
        order = SimpleNamespace(order_id="o", status="paid", negotiation_id="n1",
                                service_id=service_id)
        client = FakeClient(negotiation=_neg("{}", nid="n1"), order=order)
        p = CrooProvider(client, pipeline=sig, services={"EVENTS": evt},
                         deliver_factory=lambda payload: payload)
        outcome, _ = asyncio.run(p.handle_paid("o"))
        assert outcome == "delivered"
        return client.delivered[0][1]["schema"]

    assert deliver_for("EVENTS") == "catalyst.events"     # routed to events
    assert deliver_for("OTHER") == "catalyst.signals"     # unknown → default
    assert deliver_for(None) == "catalyst.signals"        # missing → default


def test_events_service_negotiation_skips_coverage():
    """The events feed serves the whole market, so a covered-assets provider must
    NOT reject an events order for an uncovered asset."""
    neg = SimpleNamespace(negotiation_id="n1", requirements='{"assets": "DOGE"}',
                          service_id="EVENTS")
    client = FakeClient(negotiation=neg)
    p = CrooProvider(client, covered_assets=["BTC"], services={"EVENTS": lambda req: {}},
                     health=lambda: (True, "ok"))
    outcome, _ = asyncio.run(p.handle_negotiation("n1"))
    assert outcome == "accepted"                          # coverage skipped for events


def test_parse_requirements():
    assert parse_requirements("") == {}
    assert parse_requirements('{"assets": ["BTC"]}') == {"assets": ["BTC"]}
    assert parse_requirements("{bad json") is None     # present but invalid → reject
    assert parse_requirements("[1,2]") is None          # not an object → reject


# ---- accept / reject gate ---------------------------------------------------

def test_accepts_healthy_covered_negotiation():
    client = FakeClient(negotiation=_neg('{"assets": ["BTC"]}'))
    p = CrooProvider(client, covered_assets=["BTC", "ETH"], health=lambda: (True, "ok"))
    outcome, _ = asyncio.run(p.handle_negotiation("neg1"))
    assert outcome == "accepted"
    assert client.accepted == ["neg1"] and client.rejected == []


def test_rejects_unparseable_requirements():
    client = FakeClient(negotiation=_neg("{bad json"))
    p = CrooProvider(client, health=lambda: (True, "ok"))
    outcome, reason = asyncio.run(p.handle_negotiation("neg1"))
    assert outcome == "rejected" and "unparseable" in reason
    assert client.rejected and client.accepted == []


def test_rejects_uncovered_assets():
    client = FakeClient(negotiation=_neg('{"assets": ["DOGE"]}'))
    p = CrooProvider(client, covered_assets=["BTC", "ETH"], health=lambda: (True, "ok"))
    outcome, reason = asyncio.run(p.handle_negotiation("neg1"))
    assert outcome == "rejected" and "unsupported assets" in reason


def test_rejects_when_unhealthy():
    client = FakeClient(negotiation=_neg('{"assets": ["BTC"]}'))
    p = CrooProvider(client, covered_assets=["BTC"], health=lambda: (False, "error streak 3"))
    outcome, reason = asyncio.run(p.handle_negotiation("neg1"))
    assert outcome == "rejected" and "unhealthy" in reason


# ---- delivery ---------------------------------------------------------------

def test_delivers_pipeline_payload_filtered_by_requirements():
    client = FakeClient(order=_order(), negotiation=_neg('{"assets": ["ETH"]}'))
    seen = {}

    def pipeline(req):
        seen["req"] = req
        return {"schema": "catalyst.signals", "count": 1, "actions": [{"asset": "ETH"}]}

    p = CrooProvider(client, pipeline=pipeline, deliver_factory=_capture_factory)
    outcome, _ = asyncio.run(p.handle_paid("ord1"))
    assert outcome == "delivered"
    assert seen["req"] == {"assets": ["ETH"]}                 # requirements reached the pipeline
    (oid, request) = client.delivered[0]
    assert oid == "ord1"
    assert request["schema"]["actions"][0]["asset"] == "ETH"  # canonical payload delivered


def test_paid_handler_is_idempotent_on_redelivery():
    client = FakeClient(order=_order(), negotiation=_neg("{}"))
    p = CrooProvider(client, pipeline=lambda req: {"count": 0, "actions": []},
                     deliver_factory=_capture_factory)
    first, _ = asyncio.run(p.handle_paid("ord1"))
    second, reason = asyncio.run(p.handle_paid("ord1"))   # reconnect redelivers ORDER_PAID
    assert first == "delivered" and second == "skipped"
    assert len(client.delivered) == 1                      # never double-delivered


def test_paid_handler_skips_already_delivering_order():
    # A reconnect where the order already moved past paid on-chain.
    client = FakeClient(order=_order(status="completed"), negotiation=_neg("{}"))
    p = CrooProvider(client, pipeline=lambda req: {"count": 0}, deliver_factory=_capture_factory)
    outcome, reason = asyncio.run(p.handle_paid("ord1"))
    assert outcome == "skipped" and "completed" in reason
    assert client.delivered == []


# ---- no-op provider + full-loop end-to-end (fake in-process stream) ---------

def test_no_op_pipeline_is_canonical_and_echoes_requirements():
    payload = no_op_pipeline({"assets": ["ETH"], "horizon": "intraday"})
    assert payload["schema"] == "catalyst.signals"          # real deliverable shape
    assert payload["disclaimer"]
    assert payload["mode"] == "no-op"                        # meta dissolved to top level
    assert payload["requirements"] == {"assets": ["ETH"], "horizon": "intraday"}
    assert payload["universe"] == ["BTC"]                    # required by registered schema
    # single flat signal object matching the registered schema
    assert payload["actions"]["asset"] == "BTC"
    assert payload["actions"]["signal"] in ("alert", "watch")
    assert "freshness" in payload["actions"]                 # not freshness_minutes
    assert payload["catalysts"] == ["__no_op_probe__"]       # flat array; clearly a probe


class FakeStream:
    """Mimics croo.EventStream: register sync handlers, dispatch events by type."""

    def __init__(self):
        self.handlers = {}

    def on(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    def emit(self, event):
        for h in self.handlers.get(event.type, []):
            h(event)


class FakeWSClient(FakeClient):
    async def connect_websocket(self):
        self.stream = FakeStream()
        return self.stream


async def _drain():
    """Let the sync WS callbacks' create_task()'d handlers (incl. to_thread) finish."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


def test_no_op_provider_full_loop_accepts_and_delivers():
    """End-to-end through start() → dispatch → _on_* → to_thread → deliver, no network.

    Uses the *real* deliver_factory (constructs the croo SDK's DeliverOrderRequest),
    so this proves the whole provider wiring short of the live backend."""
    from croo import EventType

    async def go():
        client = FakeWSClient(
            negotiation=_neg('{"assets": ["BTC"]}'), order=_order(status="paid"),
        )
        provider = make_no_op_provider(client)
        stream = await provider.start()

        stream.emit(SimpleNamespace(
            type=EventType.NEGOTIATION_CREATED, negotiation_id="neg1", order_id=""))
        await _drain()
        assert client.accepted == ["neg1"] and client.rejected == []

        stream.emit(SimpleNamespace(type=EventType.ORDER_PAID, order_id="ord1"))
        await _drain()
        assert len(client.delivered) == 1
        oid, request = client.delivered[0]
        assert oid == "ord1"
        # real SDK DeliverOrderRequest carrying the canonical no-op payload
        assert request.deliverable_type
        assert '"mode": "no-op"' in request.deliverable_schema

    asyncio.run(go())


# ---- run() watchdog: a dead stream must crash the process -------------------

class DeadableStream(FakeStream):
    """FakeStream + the health surface run()'s watchdog polls."""

    def __init__(self, error=None, tasks=None):
        super().__init__()
        self._error = error
        self._tasks = tasks

    def err(self):
        return self._error


def _watchdog_provider(stream):
    client = FakeClient()
    client.connect_websocket = lambda: _async_return(stream)
    provider = make_no_op_provider(client)
    provider.WATCHDOG_INTERVAL = 0.01
    return provider


async def _async_return(value):
    return value


def test_run_raises_when_stream_records_fatal_error():
    """Duplicate SDK-Key (1008): SDK sets err() and stops — run() must not idle."""
    stream = DeadableStream(error=RuntimeError("duplicate SDK-Key connection"),
                            tasks=[])
    with pytest.raises(RuntimeError, match="duplicate SDK-Key"):
        asyncio.run(_watchdog_provider(stream).run())


def test_run_raises_when_all_stream_tasks_are_dead():
    """Failed SDK reconnect leaves no live tasks and no err() — still must exit."""
    stream = DeadableStream(error=None, tasks=[])
    with pytest.raises(RuntimeError, match="no live websocket tasks"):
        asyncio.run(_watchdog_provider(stream).run())


def test_run_keeps_waiting_when_stream_health_is_opaque():
    """A stream without _tasks (fake/newer SDK) must not be declared dead."""
    async def go():
        stream = DeadableStream(error=None, tasks=None)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_watchdog_provider(stream).run(), timeout=0.2)

    asyncio.run(go())


# ---- default pipeline over a real store -------------------------------------

def test_default_pipeline_produces_filtered_canonical_payload(tmp_path):
    db = str(tmp_path / "c.db")
    conn = open_store(db)
    try:
        save_posts(conn, [
            Post(source="bluesky", uri="p1", text="$BTC ETF approved, price soars",
                 indexed_at="2026-07-01T11:59:00Z", author=Author(handle="watcher.guru"),
                 metrics=Metrics(likes=10)),
            Post(source="bluesky", uri="p2", text="$ETH looks weak, selloff continues",
                 indexed_at="2026-07-01T11:59:00Z", author=Author(handle="rando"),
                 metrics=Metrics()),
        ])
        from catalyst.enrich import hybrid_enrich
        from catalyst.store import fetch_unenriched
        save_enrichments(conn, hybrid_enrich(fetch_unenriched(conn),
                                             primary_handles=frozenset({"watcher.guru"})))
    finally:
        conn.close()

    payload = default_pipeline(db, {"assets": ["BTC"]})
    assert payload["schema"] == "catalyst.signals"
    assert isinstance(payload["actions"], dict)                    # single flat signal object
    assert "universe" in payload                                   # required by registered schema
    assert payload["requirements"] == {"assets": ["BTC"]}          # meta lifted to top level
    assert payload["disclaimer"]                                   # proposal disclaimer present
    assert payload["window_hours"] == 24.0                         # default lookback echoed
    if payload["actions"]:                                         # if a signal was produced
        assert payload["actions"]["asset"] == "BTC"                # filtered to requirements
        assert payload["actions"]["signal"] in ("alert", "watch")


def test_default_pipeline_window_widens_history(tmp_path):
    """A buyer's lookback (hours → a week) changes how far back the signal layer
    reads: a 5-day-old catalyst only enters the universe under the wider window."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    db = str(tmp_path / "w.db")
    conn = open_store(db)
    try:
        save_posts(conn, [
            Post(source="bluesky", uri="fresh", text="$ETH ETF approved, price soars",
                 indexed_at=fresh, author=Author(handle="watcher.guru"), metrics=Metrics(likes=10)),
            Post(source="bluesky", uri="old", text="$BTC ETF approved, price soars",
                 indexed_at=old, author=Author(handle="watcher.guru"), metrics=Metrics(likes=10)),
        ])
        from catalyst.enrich import hybrid_enrich
        from catalyst.store import fetch_unenriched
        save_enrichments(conn, hybrid_enrich(fetch_unenriched(conn),
                                             primary_handles=frozenset({"watcher.guru"})))
    finally:
        conn.close()

    narrow = default_pipeline(db, {})                 # default 24h
    wide = default_pipeline(db, {"window": "7d"})      # up to a week

    assert narrow["window_hours"] == 24.0
    assert wide["window_hours"] == 168.0
    # The 5-day-old BTC catalyst is out of the 24h window, in under the 7d one.
    assert "BTC" not in narrow["universe"]
    assert "BTC" in wide["universe"]
    assert "ETH" in wide["universe"]                   # fresh post is in both


def test_default_pipeline_merges_grounded_narration(tmp_path):
    """When a `present` callable is supplied, its prose fields land on the payload
    and the computed numbers are untouched. A presenter failure is swallowed."""
    from datetime import datetime, timedelta, timezone
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db = str(tmp_path / "n.db")
    conn = open_store(db)
    try:
        save_posts(conn, [
            Post(source="bluesky", uri="p1", text="$BTC ETF approved, price soars",
                 indexed_at=fresh, author=Author(handle="watcher.guru"),
                 metrics=Metrics(likes=10)),
        ])
        from catalyst.enrich import hybrid_enrich
        from catalyst.store import fetch_unenriched
        save_enrichments(conn, hybrid_enrich(fetch_unenriched(conn),
                                             primary_handles=frozenset({"watcher.guru"})))
    finally:
        conn.close()

    seen = {}

    def stub_present(flat, headlines=None):
        # a grounded presenter never returns numbers — only prose fields. It is
        # handed the real catalyst headlines behind the top signal.
        seen["headlines"] = headlines
        return {"summary": "grounded one-liner", "catalyst_notes": {"etf": "spot-ETF news"}}

    got = default_pipeline(db, {}, present=stub_present)
    assert got["summary"] == "grounded one-liner"
    assert got["catalyst_notes"] == {"etf": "spot-ETF news"}
    assert got["schema"] == "catalyst.signals"        # deterministic envelope intact
    assert isinstance(got["actions"], dict)
    # the presenter received the actual post text (what happened), not just tags
    assert seen["headlines"] and any("ETF" in h["text"] for h in seen["headlines"])

    def boom(flat, headlines=None):
        raise RuntimeError("llm down")

    safe = default_pipeline(db, {}, present=boom)      # must not raise
    assert "summary" not in safe                        # delivered without narrative
    assert safe["schema"] == "catalyst.signals"
