"""Trend layer — slope of a layer's bias over accumulated history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from catalyst.store import open_store, save_bias_snapshots
from catalyst.trend import compute_trend_bias


def _seed(conn, asset, biases, *, layer="flows", start=None, step_days=1.0):
    """Write one snapshot per bias value, one `step_days` apart."""
    start = start or datetime(2026, 7, 1, tzinfo=timezone.utc)
    for i, b in enumerate(biases):
        ts = (start + timedelta(days=i * step_days)).isoformat()
        obj = SimpleNamespace(bias=b, label="x", evidence=1.0)
        kw = {"flow_bias": {asset: obj}} if layer == "flows" else {"supply_bias": {asset: obj}}
        save_bias_snapshots(conn, ts, **kw)
    return start + timedelta(days=(len(biases) - 1) * step_days)


def test_rising_series_is_strengthening_positive():
    conn = open_store(":memory:")
    try:
        end = _seed(conn, "BTC", [0.0, 0.1, 0.2, 0.3, 0.4])
        t = compute_trend_bias(conn, ["BTC"], now=end + timedelta(hours=1))["BTC"]
        assert t.bias > 0 and t.label == "strengthening"
        assert t.evidence == 5
    finally:
        conn.close()


def test_falling_series_is_weakening_negative():
    conn = open_store(":memory:")
    try:
        end = _seed(conn, "ETH", [0.5, 0.3, 0.1, -0.1, -0.3])
        t = compute_trend_bias(conn, ["ETH"], now=end + timedelta(hours=1))["ETH"]
        assert t.bias < 0 and t.label == "weakening"
    finally:
        conn.close()


def test_flat_series_is_flat():
    conn = open_store(":memory:")
    try:
        end = _seed(conn, "SOL", [0.2, 0.2, 0.2, 0.2])
        t = compute_trend_bias(conn, ["SOL"], now=end + timedelta(hours=1))["SOL"]
        assert t.label == "flat" and abs(t.bias) < 0.1
    finally:
        conn.close()


def test_thin_history_is_omitted_not_flat():
    """Fewer than min_points snapshots → no modifier at all (cold start)."""
    conn = open_store(":memory:")
    try:
        end = _seed(conn, "ARB", [0.1, 0.2])          # only 2 points
        out = compute_trend_bias(conn, ["ARB"], now=end + timedelta(hours=1))
        assert "ARB" not in out
    finally:
        conn.close()


def test_trend_boosts_aligned_buy_and_damps_opposed():
    """The done-check: a rising trend boosts an aligned buy's confidence, a falling
    one damps it, and the trend shows up in the action's `layers`."""
    from catalyst.planner import plan
    from catalyst.signals import Signal

    def sig(asset="BTC", score=0.5):
        return Signal(asset=asset, sentiment=score, strength=0.6, score=score,
                      direction="bullish", mentions=3, velocity=1.0,
                      catalysts=[], latest_at=None, sample=["t"])

    base = plan([sig()])[0].confidence
    up = plan([sig()], trend_bias={"BTC": SimpleNamespace(bias=0.5, label="strengthening")})[0]
    down = plan([sig()], trend_bias={"BTC": SimpleNamespace(bias=-0.5, label="weakening")})[0]
    assert up.confidence > base > down.confidence
    assert up.layers["trend"]["effect"] == "boost" and up.layers["trend"]["label"] == "strengthening"
    assert down.layers["trend"]["effect"] == "damp"


def test_point_in_time_excludes_future_and_window():
    """Only snapshots with ts <= now and inside the window count (no lookahead)."""
    conn = open_store(":memory:")
    try:
        # rising for 5 days; evaluate as-of day 2 → only 3 points seen, no future
        _seed(conn, "BTC", [0.0, 0.1, 0.2, 0.3, 0.4])
        asof = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)   # after the 3rd point
        t = compute_trend_bias(conn, ["BTC"], now=asof)["BTC"]
        assert t.evidence == 3                                  # future points excluded
        # window clip: a very short window drops the oldest points
        t2 = compute_trend_bias(conn, ["BTC"], now=asof, window_days=1.0)
        assert t2.get("BTC") is None or t2["BTC"].evidence < 3
    finally:
        conn.close()
