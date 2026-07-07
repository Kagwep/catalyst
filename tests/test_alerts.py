"""Alerts — rules, pluggable sinks, de-dupe, and fail-soft dispatch."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from catalyst.alerts import (
    AlertRule,
    FileSink,
    Sink,
    StderrSink,
    build_alerting,
    dispatch,
)
from catalyst.planner import Action
from catalyst.store import fetch_recent_alerts, open_store

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _act(asset, action="buy", *, confidence=0.7, catalysts=None):
    return Action(asset=asset, action=action, direction="bullish", confidence=confidence,
                  horizon="short", score=0.5, rationale="r", catalysts=catalysts or [],
                  created_at=NOW.isoformat())


class _CaptureSink(Sink):
    name = "capture"

    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        return True


class _BoomSink(Sink):
    name = "boom"

    def send(self, payload):
        raise RuntimeError("sink down")


# ---- rules ------------------------------------------------------------------

def test_rule_filters_by_confidence_and_action():
    rule = AlertRule(min_confidence=0.5, actions=frozenset({"buy", "sell"}))
    assert rule.allows(_act("BTC", confidence=0.7), NOW)
    assert not rule.allows(_act("BTC", confidence=0.3), NOW)   # too weak
    assert not rule.allows(_act("BTC", "watch", confidence=0.9), NOW)  # action not allowed


def test_rule_per_asset_override_and_catalyst_and_quiet_hours():
    rule = AlertRule(min_confidence=0.5, per_asset={"BTC": {"min_confidence": 0.9}},
                     catalysts=frozenset({"etf"}), quiet_hours=(0, 8))
    # BTC needs 0.9 by override
    assert not rule.allows(_act("BTC", confidence=0.7, catalysts=["etf"]), NOW)
    assert rule.allows(_act("BTC", confidence=0.95, catalysts=["etf"]), NOW)
    # catalyst filter: no matching catalyst → blocked
    assert not rule.allows(_act("ETH", confidence=0.95, catalysts=["hack"]), NOW)
    # quiet hours (03:00 UTC) suppress everything
    three_am = NOW.replace(hour=3)
    assert not rule.allows(_act("ETH", confidence=0.95, catalysts=["etf"]), three_am)


# ---- dispatch ---------------------------------------------------------------

def test_dispatch_delivers_and_filters(tmp_path):
    conn = open_store(str(tmp_path / "a.db"))
    cap = _CaptureSink()
    rule = AlertRule(min_confidence=0.5)
    try:
        res = dispatch([_act("BTC", confidence=0.8), _act("DOGE", confidence=0.2)],
                       rules=rule, sinks=[cap], conn=conn, now=NOW)
        assert len(res.delivered) == 1 and res.filtered == 1
        # one payload, canonical shape, one action
        assert cap.payloads[0]["schema"] == "catalyst.signals"
        assert cap.payloads[0]["actions"][0]["asset"] == "BTC"
        # recorded to history
        assert len(fetch_recent_alerts(conn, within_minutes=120, now=NOW)) == 1
    finally:
        conn.close()


def test_dispatch_dedupes_across_restarts(tmp_path):
    db = str(tmp_path / "a.db")
    rule = AlertRule(min_confidence=0.5, cooldown_minutes=60)

    conn = open_store(db)
    try:
        dispatch([_act("BTC", confidence=0.8)], rules=rule, sinks=[StderrSink()], conn=conn, now=NOW)
    finally:
        conn.close()

    # New connection (simulates a restart): the same alert 30m later is suppressed.
    conn = open_store(db)
    try:
        cap = _CaptureSink()
        res = dispatch([_act("BTC", confidence=0.8)], rules=rule, sinks=[cap],
                       conn=conn, now=NOW + timedelta(minutes=30))
        assert res.delivered == [] and res.suppressed == 1
        assert cap.payloads == []
        # ...but past the cooldown it fires again.
        res2 = dispatch([_act("BTC", confidence=0.8)], rules=rule, sinks=[cap],
                        conn=conn, now=NOW + timedelta(minutes=90))
        assert len(res2.delivered) == 1
    finally:
        conn.close()


def test_dispatch_is_fail_soft_and_records_on_partial_success(tmp_path):
    conn = open_store(str(tmp_path / "a.db"))
    cap = _CaptureSink()
    try:
        res = dispatch([_act("BTC", confidence=0.8)], rules=AlertRule(min_confidence=0.5),
                       sinks=[_BoomSink(), cap], conn=conn, now=NOW)
        assert res.sink_results == {"boom": False, "capture": True}
        assert len(res.delivered) == 1                       # good sink still delivered
        assert len(fetch_recent_alerts(conn, within_minutes=120, now=NOW)) == 1
    finally:
        conn.close()


def test_file_sink_writes_jsonl(tmp_path):
    path = tmp_path / "alerts.jsonl"
    dispatch([_act("BTC", confidence=0.8)], rules=AlertRule(min_confidence=0.5),
             sinks=[FileSink(str(path))], now=NOW)
    line = path.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["actions"][0]["asset"] == "BTC"


# ---- config -----------------------------------------------------------------

def test_build_alerting_defaults_and_from_config():
    rule, sinks = build_alerting(None)
    assert [s.name for s in sinks] == ["stderr"]
    assert rule.actions == frozenset({"buy", "sell"})

    rule2, sinks2 = build_alerting({
        "min_confidence": 0.6, "cooldown_minutes": 30,
        "sinks": [{"type": "webhook", "url": "https://example.com/hook"}, {"type": "stderr"}],
    })
    assert rule2.min_confidence == 0.6 and rule2.cooldown_minutes == 30
    assert [s.name for s in sinks2] == ["webhook", "stderr"]
