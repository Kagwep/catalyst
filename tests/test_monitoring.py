"""Monitoring — cycle-health persistence, liveness detection, status surface."""

from __future__ import annotations

from datetime import datetime, timezone

from catalyst.monitoring import (
    CycleHealth,
    detect_issues,
    issues_to_actions,
    status_report,
)
from catalyst.store import fetch_recent_health, open_store, save_cycle_health

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _h(cycle, *, per_source=None, error=None, duration_ms=100.0, llm_calls=0):
    return CycleHealth(cycle=cycle, started_at=NOW.isoformat(), duration_ms=duration_ms,
                       fetched=sum((per_source or {}).values()), per_source=per_source or {},
                       error=error, llm_calls=llm_calls, summary=f"cycle {cycle}")


# ---- persistence ------------------------------------------------------------

def test_save_and_fetch_cycle_health(tmp_path):
    conn = open_store(str(tmp_path / "m.db"))
    try:
        save_cycle_health(conn, _h(1, per_source={"rss": 3, "bluesky": 5}))
        save_cycle_health(conn, _h(2, per_source={"rss": 2}))
        hist = fetch_recent_health(conn, limit=10)
        assert [h["cycle"] for h in hist] == [2, 1]     # newest-first
        assert hist[1]["per_source"] == {"rss": 3, "bluesky": 5}  # decoded to a dict
    finally:
        conn.close()


# ---- detection --------------------------------------------------------------

def test_source_silence_detected_after_k_cycles():
    # rss produced early, then 0 for the last 3 cycles → silent. bluesky still live.
    history = [
        _h(5, per_source={"bluesky": 4}),
        _h(4, per_source={"bluesky": 3}),
        _h(3, per_source={"bluesky": 5}),
        _h(2, per_source={"rss": 2, "bluesky": 4}),
        _h(1, per_source={"rss": 3, "bluesky": 4}),
    ]
    issues = detect_issues(history, silence_cycles=3)
    kinds = {(i.kind, i.subject) for i in issues}
    assert ("source_silent", "rss") in kinds
    assert ("source_silent", "bluesky") not in kinds


def test_error_streak_detected():
    history = [_h(3, error="boom"), _h(2, error="boom"), _h(1, error="boom")]
    issues = detect_issues(history, max_error_streak=3)
    assert any(i.kind == "error_streak" for i in issues)
    # a broken streak (latest ok) does not fire
    ok = detect_issues([_h(4)] + history, max_error_streak=3)
    assert not any(i.kind == "error_streak" for i in ok)


def test_slow_cycle_and_llm_budget():
    slow = detect_issues([_h(1, duration_ms=20_000)], interval_seconds=5, slow_factor=1.5)
    assert any(i.kind == "slow_cycle" for i in slow)
    budget = detect_issues([_h(1, llm_calls=200)], llm_call_ceiling=100)
    assert any(i.kind == "llm_budget" for i in budget)


def test_no_issues_when_healthy():
    history = [_h(3, per_source={"rss": 2}), _h(2, per_source={"rss": 2}), _h(1, per_source={"rss": 2})]
    assert detect_issues(history, interval_seconds=300, silence_cycles=3) == []


def test_issues_become_ops_actions():
    issues = detect_issues([_h(3, error="x"), _h(2, error="x"), _h(1, error="x")], max_error_streak=3)
    acts = issues_to_actions(issues, now=NOW)
    a = acts[0]
    assert a.action == "ops" and a.asset.startswith("OPS:")
    assert "ops" in a.catalysts


# ---- ops alerts ride the Phase-3 sinks --------------------------------------

def test_ops_alerts_dispatch_through_sinks(tmp_path):
    from catalyst.alerts import Sink, dispatch
    from catalyst.monitoring import OPS_RULE

    class _Cap(Sink):
        name = "cap"

        def __init__(self):
            self.payloads = []

        def send(self, payload):
            self.payloads.append(payload)
            return True

    conn = open_store(str(tmp_path / "m.db"))
    cap = _Cap()
    try:
        acts = issues_to_actions(
            detect_issues([_h(3, error="x"), _h(2, error="x"), _h(1, error="x")], max_error_streak=3),
            now=NOW)
        res = dispatch(acts, rules=OPS_RULE, sinks=[cap], conn=conn, now=NOW)
        assert len(res.delivered) == 1
        assert cap.payloads[0]["actions"][0]["signal"] == "ops"
    finally:
        conn.close()


# ---- status surface ---------------------------------------------------------

def test_status_report_reflects_reality(tmp_path):
    conn = open_store(str(tmp_path / "m.db"))
    try:
        save_cycle_health(conn, _h(1, per_source={"rss": 3}))
        rep = status_report(conn, now=NOW, interval_seconds=300)
        assert rep["last_cycle"]["cycle"] == 1
        assert rep["healthy"] is True
        assert rep["error_streak"] == 0
    finally:
        conn.close()
