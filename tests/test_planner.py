from datetime import datetime, timedelta, timezone

from catalyst.planner import plan
from catalyst.signals import Signal

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def sig(asset, score, *, sentiment=None, strength=0.6, direction=None, mentions=3,
        velocity=1.0, catalysts=None, age_min=10.0):
    sentiment = score if sentiment is None else sentiment
    direction = ("bullish" if sentiment > 0.1 else "bearish" if sentiment < -0.1 else "neutral") \
        if direction is None else direction
    return Signal(
        asset=asset, sentiment=sentiment, strength=strength, score=score, direction=direction,
        mentions=mentions, velocity=velocity, catalysts=catalysts or [],
        latest_at=(NOW - timedelta(minutes=age_min)).isoformat(), sample=["t"],
    )


def test_buy_sell_watch_and_skip():
    sigs = [
        sig("BTC", 0.5),                      # buy
        sig("ETH", -0.5),                     # sell
        sig("SOL", 0.0, strength=0.7, catalysts=["hack"]),  # neutral + high-impact -> watch
        sig("DOGE", 0.03),                    # below watch_threshold -> skipped
    ]
    actions = {a.asset: a for a in plan(sigs, now=NOW)}
    assert actions["BTC"].action == "buy"
    assert actions["ETH"].action == "sell"
    assert actions["SOL"].action == "watch"
    assert "DOGE" not in actions


def test_confidence_and_horizon():
    a = plan([sig("BTC", 0.6, strength=0.8, catalysts=["etf"], velocity=3.0)], now=NOW)[0]
    assert 0 < a.confidence <= 1.0
    assert a.horizon == "intraday"            # fast catalyst / high velocity
    b = plan([sig("ADA", 0.3, strength=0.5, velocity=1.0)], now=NOW)[0]
    assert b.horizon == "short"


def test_swing_horizon_on_persistent_trend():
    from types import SimpleNamespace
    strong = {"ADA": SimpleNamespace(bias=0.5, label="strengthening")}
    # non-fast signal + persistent multi-day trend → swing
    a = plan([sig("ADA", 0.3, velocity=1.0)], now=NOW, trend_bias=strong)[0]
    assert a.horizon == "swing"
    # weak/flat trend stays short
    weak = {"ADA": SimpleNamespace(bias=0.05, label="flat")}
    b = plan([sig("ADA", 0.3, velocity=1.0)], now=NOW, trend_bias=weak)[0]
    assert b.horizon == "short"
    # fast signal stays intraday even with a persistent trend (fast wins)
    c = plan([sig("BTC", 0.5, catalysts=["etf"])], now=NOW,
             trend_bias={"BTC": SimpleNamespace(bias=0.9, label="strengthening")})[0]
    assert c.horizon == "intraday"


def test_swing_uses_looser_staleness_gate():
    from types import SimpleNamespace
    strong = {"ADA": SimpleNamespace(bias=0.5, label="strengthening")}
    old = sig("ADA", 0.5, velocity=1.0, age_min=200.0)     # 200 min old
    # stale under max_age=120, but swing_max_age=6000 keeps the buy alive
    a = plan([old], now=NOW, trend_bias=strong, max_age_minutes=120,
             swing_max_age_minutes=6000)[0]
    assert a.horizon == "swing" and a.action == "buy"
    # without a swing gate it falls back to max_age → stale → downgraded to watch
    b = plan([old], now=NOW, trend_bias=strong, max_age_minutes=120)[0]
    assert b.action == "watch"


def test_stale_signal_downgrades_to_watch():
    fresh = plan([sig("BTC", 0.5, age_min=10)], now=NOW, max_age_minutes=180)[0]
    stale = plan([sig("BTC", 0.5, age_min=600)], now=NOW, max_age_minutes=180)[0]
    assert fresh.action == "buy"
    assert stale.action == "watch"
    assert "STALE" in stale.rationale


def test_cooldown_suppresses_repeat():
    recent = [{"asset": "BTC", "action": "buy", "created_at": (NOW - timedelta(minutes=30)).isoformat()}]
    suppressed = plan([sig("BTC", 0.5)], now=NOW, recent_actions=recent, cooldown_minutes=120)
    assert suppressed == []
    # Outside the cooldown window it fires again.
    old = [{"asset": "BTC", "action": "buy", "created_at": (NOW - timedelta(minutes=200)).isoformat()}]
    assert plan([sig("BTC", 0.5)], now=NOW, recent_actions=old, cooldown_minutes=120)[0].action == "buy"


def test_min_confidence_filter():
    assert plan([sig("BTC", 0.5, strength=0.6)], now=NOW, min_confidence=0.99) == []


# ---- Phase 2b gates ---------------------------------------------------------

from dataclasses import dataclass


@dataclass
class _Bias:
    bias: float
    label: str


def test_per_horizon_staleness_expires_fast_catalysts_sooner():
    # A hack (fast/intraday) at 90m: fresh under the 180m short limit, but stale
    # under a 60m fast limit → downgraded to watch.
    s = sig("BTC", 0.5, catalysts=["hack"], age_min=90)
    assert plan([s], now=NOW, max_age_minutes=180)[0].action == "buy"
    downgraded = plan([s], now=NOW, max_age_minutes=180, fast_max_age_minutes=60)[0]
    assert downgraded.action == "watch"
    assert downgraded.horizon == "intraday"


def test_cooldown_breaks_on_materially_higher_confidence():
    s = sig("BTC", 0.5, strength=0.6)   # confidence ≈ 0.54
    weak_prior = [{"asset": "BTC", "action": "buy", "confidence": 0.30,
                   "created_at": (NOW - timedelta(minutes=30)).isoformat()}]
    strong_prior = [{"asset": "BTC", "action": "buy", "confidence": 0.50,
                     "created_at": (NOW - timedelta(minutes=30)).isoformat()}]
    # +0.24 over the weak prior → breaks the cooldown; +0.04 over the strong one → suppressed.
    assert plan([s], now=NOW, recent_actions=weak_prior, cooldown_minutes=120)[0].action == "buy"
    assert plan([s], now=NOW, recent_actions=strong_prior, cooldown_minutes=120) == []


def test_conflict_downgrades_to_watch():
    # Bullish signal, but the market layer is strongly bearish → net opposition
    # exceeds the conflict margin → watch, not a weak buy.
    a = plan([sig("BTC", 0.5)], now=NOW,
             market_bias={"BTC": _Bias(-0.9, "bearish-momentum")})[0]
    assert a.action == "watch"
    assert "CONFLICT" in a.rationale
    assert a.layers["market"]["effect"] == "damp"


def test_aligned_layers_do_not_conflict():
    a = plan([sig("BTC", 0.5)], now=NOW,
             market_bias={"BTC": _Bias(0.9, "bullish-momentum")})[0]
    assert a.action == "buy"
    assert a.layers["market"]["effect"] == "boost"


def test_actions_persist_and_feed_cooldown(tmp_path):
    from catalyst.store import fetch_recent_actions, open_store, save_actions

    conn = open_store(str(tmp_path / "t.db"))
    try:
        actions = plan([sig("BTC", 0.5)], now=NOW)
        assert save_actions(conn, actions) == 1
        recent = fetch_recent_actions(conn, within_minutes=240, now=NOW)
        assert recent[0]["asset"] == "BTC" and recent[0]["action"] == "buy"
        # That recent action now suppresses a repeat via the planner cooldown.
        assert plan([sig("BTC", 0.5)], now=NOW, recent_actions=recent, cooldown_minutes=120) == []
    finally:
        conn.close()


# ---- Phase 8b: confidence calibration ---------------------------------------

def test_confidence_calibration_remaps_final_confidence():
    from catalyst.planner import apply_confidence_calibration

    # A stated→realized table that says "our 0.8-ish confidences really win ~0.5".
    table = [[0.4, 0.4], [0.8, 0.5]]
    raw = plan([sig("BTC", 0.6, strength=0.8)], now=NOW)[0]
    cal = plan([sig("BTC", 0.6, strength=0.8)], now=NOW, confidence_calibration=table)[0]
    assert cal.confidence != raw.confidence
    # It equals the piecewise-linear map applied to the raw (pre-round) confidence.
    assert abs(cal.confidence - round(apply_confidence_calibration(raw.confidence, table), 3)) < 1e-6
    assert 0.0 <= cal.confidence <= 1.0


def test_confidence_calibration_absent_is_noop():
    raw = plan([sig("BTC", 0.6, strength=0.8)], now=NOW)[0]
    same = plan([sig("BTC", 0.6, strength=0.8)], now=NOW, confidence_calibration=None)[0]
    assert raw.confidence == same.confidence


def test_apply_confidence_calibration_clamps_and_interpolates():
    from catalyst.planner import apply_confidence_calibration

    table = [[0.2, 0.1], [0.6, 0.5], [0.9, 0.95]]
    assert apply_confidence_calibration(0.0, table) == 0.1     # below range → first realized
    assert apply_confidence_calibration(1.0, table) == 0.95    # above range → last realized
    mid = apply_confidence_calibration(0.4, table)             # halfway 0.2→0.6 → halfway 0.1→0.5
    assert abs(mid - 0.3) < 1e-9
