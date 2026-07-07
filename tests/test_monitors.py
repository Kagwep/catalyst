"""Monitors — named catalyst-scoped watches: matching, both trigger paths,
per-monitor de-dupe, routing, and the config round-trip."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from catalyst.alerts import Sink
from catalyst.monitors import (
    Monitor,
    build_monitors,
    evaluate,
    load_specs,
    monitor_from_spec,
    run_monitors,
    save_specs,
)
from catalyst.payload import build_event_payload
from catalyst.planner import Action
from catalyst.store import fetch_recent_monitor_fires, open_store, save_monitor_fires

NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def _act(asset, action="buy", *, confidence=0.7, horizon="short", catalysts=None):
    return Action(asset=asset, action=action, direction="bullish", confidence=confidence,
                  horizon=horizon, score=0.5, rationale="r", catalysts=catalysts or [],
                  created_at=NOW.isoformat())


def _post(uri, *, catalyst="treasury", assets=("AAVE",), age_min=1.0, source="onchain_actions"):
    return {
        "uri": uri, "catalyst": catalyst, "assets": list(assets), "source": source,
        "url": f"https://x/{uri}", "text": "a big treasury move happened",
        "sentiment_score": -0.2,
        "indexed_at": (NOW - timedelta(minutes=age_min)).isoformat(),
    }


class _Capture(Sink):
    name = "capture"

    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        return True


class _Boom(Sink):
    name = "boom"

    def send(self, payload):
        raise RuntimeError("sink down")


# --- matching -------------------------------------------------------------


def test_matches_action_criteria():
    m = Monitor(name="m", assets=frozenset({"ARB"}), catalysts=frozenset({"upgrade"}),
                actions=frozenset({"buy"}), min_confidence=0.6, horizons=frozenset({"intraday"}))
    assert m.matches_action(_act("ARB", "buy", confidence=0.7, horizon="intraday",
                                 catalysts=["upgrade"]))
    assert not m.matches_action(_act("BTC", catalysts=["upgrade"], horizon="intraday"))   # asset
    assert not m.matches_action(_act("ARB", "sell", catalysts=["upgrade"], horizon="intraday"))  # action
    assert not m.matches_action(_act("ARB", confidence=0.5, catalysts=["upgrade"], horizon="intraday"))  # conf
    assert not m.matches_action(_act("ARB", catalysts=["listing"], horizon="intraday"))   # catalyst
    assert not m.matches_action(_act("ARB", catalysts=["upgrade"], horizon="short"))       # horizon


def test_matches_event_criteria():
    m = Monitor(name="m", assets=frozenset({"AAVE"}), catalysts=frozenset({"treasury"}))
    assert m.matches_event(_post("p1", catalyst="treasury", assets=["AAVE"]))
    assert not m.matches_event(_post("p2", catalyst="upgrade", assets=["AAVE"]))   # catalyst
    assert not m.matches_event(_post("p3", catalyst="treasury", assets=["ARB"]))   # asset
    assert not m.matches_event(_post("p4", catalyst="", assets=["AAVE"]))          # no catalyst


def test_none_criteria_match_anything():
    m = Monitor(name="m")  # catalysts=None, assets=None
    assert m.matches_event(_post("p", catalyst="anything", assets=["XYZ"]))
    assert m.matches_action(_act("XYZ", "buy", catalysts=["whatever"]))


# --- proposal path --------------------------------------------------------


def test_proposal_path_fires_and_dedupes(tmp_path):
    conn = open_store(str(tmp_path / "c.db"))
    cap = _Capture()
    m = Monitor(name="arb", catalysts=frozenset({"upgrade"}), sinks=[cap], cooldown_minutes=60)
    acts = [_act("ARB", "buy", catalysts=["upgrade"]), _act("BTC", "buy", catalysts=["listing"])]

    r1 = evaluate(m, actions=acts, conn=conn, now=NOW)
    assert r1.actions_fired == 1 and r1.delivered
    assert len(cap.payloads) == 1
    assert cap.payloads[0]["actions"][0]["asset"] == "ARB"
    assert cap.payloads[0]["monitor"] == "arb"

    # Same action inside cooldown → suppressed (persisted de-dupe).
    r2 = evaluate(m, actions=acts, conn=conn, now=NOW + timedelta(minutes=5))
    assert r2.actions_fired == 0 and r2.suppressed == 1
    assert len(cap.payloads) == 1

    # After the cooldown → fires again.
    r3 = evaluate(m, actions=acts, conn=conn, now=NOW + timedelta(minutes=61))
    assert r3.actions_fired == 1
    conn.close()


# --- event path -----------------------------------------------------------


def test_event_path_fires_dedupes_and_skips_stale(tmp_path):
    conn = open_store(str(tmp_path / "c.db"))
    cap = _Capture()
    m = Monitor(name="aave-treasury", catalysts=frozenset({"treasury"}),
                assets=frozenset({"AAVE"}), sinks=[cap], cooldown_minutes=60)
    posts = [
        _post("fresh", catalyst="treasury", assets=["AAVE"], age_min=5),
        _post("stale", catalyst="treasury", assets=["AAVE"], age_min=200),   # older than cooldown
        _post("other", catalyst="upgrade", assets=["AAVE"], age_min=5),      # wrong catalyst
    ]

    r1 = evaluate(m, posts=posts, conn=conn, now=NOW)
    assert r1.events_fired == 1
    assert cap.payloads[0]["events"][0]["uri"] == "fresh"
    assert cap.payloads[0]["schema"] == "catalyst.events"

    # Same uri again → suppressed by the per-uri de-dupe.
    r2 = evaluate(m, posts=posts, conn=conn, now=NOW + timedelta(minutes=5))
    assert r2.events_fired == 0
    assert len(cap.payloads) == 1
    conn.close()


# --- routing / robustness -------------------------------------------------


def test_quiet_hours_suppress():
    cap = _Capture()
    m = Monitor(name="q", quiet_hours=(11, 13), sinks=[cap])
    r = evaluate(m, actions=[_act("ARB")], posts=[_post("p")], now=NOW)  # 12:00 UTC
    assert r.quiet and not cap.payloads


def test_falls_back_to_default_sinks():
    cap = _Capture()
    m = Monitor(name="d", catalysts=frozenset({"upgrade"}))  # no own sinks
    r = evaluate(m, actions=[_act("ARB", catalysts=["upgrade"])], default_sinks=[cap], now=NOW)
    assert r.actions_fired == 1 and cap.payloads


def test_dead_sink_does_not_block_and_marks_undelivered():
    m = Monitor(name="b", catalysts=frozenset({"upgrade"}), sinks=[_Boom()])
    r = evaluate(m, actions=[_act("ARB", catalysts=["upgrade"])], now=NOW)
    # matched and attempted, but no sink accepted → not recorded as delivered
    assert r.actions_fired == 0 and not r.delivered


def test_run_monitors_isolates_failures():
    good = Monitor(name="good", catalysts=frozenset({"upgrade"}), sinks=[_Capture()])
    results = run_monitors([good], actions=[_act("ARB", catalysts=["upgrade"])], now=NOW)
    assert len(results) == 1 and results[0].actions_fired == 1


# --- config round-trip ----------------------------------------------------


def test_monitor_from_spec_normalizes():
    m = monitor_from_spec({
        "name": "x", "catalysts": ["Treasury", "UNLOCK"], "assets": ["aave"],
        "on": ["event"], "actions": ["Buy"], "horizon": "Intraday",
        "min_confidence": 0.5, "cooldown_minutes": 30,
        "sinks": [{"type": "webhook", "url": "https://h"}],
    })
    assert m.catalysts == frozenset({"treasury", "unlock"})
    assert m.assets == frozenset({"AAVE"})
    assert m.on == frozenset({"event"})
    assert m.actions == frozenset({"buy"})
    assert m.horizons == frozenset({"intraday"})
    assert m.min_confidence == 0.5 and m.cooldown_minutes == 30
    assert len(m.sinks) == 1


def test_specs_roundtrip_and_load(tmp_path):
    path = str(tmp_path / "monitors.json")
    specs = [{"name": "a", "catalysts": ["treasury"], "assets": ["AAVE"]}]
    save_specs(path, specs)
    assert load_specs(path) == specs
    monitors = build_monitors(load_specs(path))
    assert len(monitors) == 1 and monitors[0].name == "a"


def test_load_specs_accepts_object_form(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"monitors": [{"name": "a"}]}), encoding="utf-8")
    assert load_specs(str(path)) == [{"name": "a"}]


def test_load_specs_missing_file(tmp_path):
    assert load_specs(str(tmp_path / "nope.json")) == []


# --- store + payload ------------------------------------------------------


def test_store_monitor_fires_roundtrip(tmp_path):
    conn = open_store(str(tmp_path / "c.db"))
    save_monitor_fires(conn, "m", "event", ["u1", "u2"], now=NOW)
    recent = fetch_recent_monitor_fires(conn, "m", "event", within_minutes=60, now=NOW + timedelta(minutes=5))
    assert recent == {"u1", "u2"}
    # outside the window → gone
    old = fetch_recent_monitor_fires(conn, "m", "event", within_minutes=60, now=NOW + timedelta(minutes=120))
    assert old == set()
    # kind is scoped
    assert fetch_recent_monitor_fires(conn, "m", "proposal", within_minutes=60, now=NOW) == set()
    conn.close()


def test_build_event_payload_shape():
    p = build_event_payload([_post("u1")], monitor="mon")
    assert p["schema"] == "catalyst.events" and p["monitor"] == "mon" and p["count"] == 1
    ev = p["events"][0]
    assert ev["uri"] == "u1" and ev["catalyst"] == "treasury" and ev["assets"] == ["AAVE"]
    assert "disclaimer" in p
