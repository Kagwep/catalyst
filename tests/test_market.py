from datetime import datetime, timedelta, timezone

from catalyst.market import (
    MarketBias,
    compute_market_bias,
    compute_technicals,
    macd_hist,
    rsi,
)
from catalyst.planner import plan
from catalyst.signals import Signal

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---- indicators -------------------------------------------------------------

def test_rsi_extremes():
    assert rsi([1, 2], period=14) is None                 # too few points
    up = rsi([float(i) for i in range(1, 40)])            # monotonic rise
    assert up is not None and up > 70                      # strong → overbought
    down = rsi([float(i) for i in range(40, 1, -1)])      # monotonic fall
    assert down < 30                                       # weak → oversold


def test_macd_hist_sign_follows_trend():
    rising = macd_hist([float(i) for i in range(1, 60)])
    falling = macd_hist([float(60 - i) for i in range(1, 60)])
    assert rising is not None and rising > 0               # uptrend → positive histogram
    assert falling < 0
    assert macd_hist([1.0, 2.0, 3.0]) is None              # too few points


# ---- per-asset technicals + market bias ------------------------------------

def _series(values, start_day=0):
    return [(int((NOW + timedelta(days=start_day + i)).timestamp()), v) for i, v in enumerate(values)]


def test_compute_technicals_uptrend_is_bullish():
    hist = {"BTC": _series([float(i) for i in range(1, 60)])}
    tech = compute_technicals(hist, now=NOW + timedelta(days=70))
    assert tech["BTC"][0] > 0                              # rising price → bullish momentum


def test_market_bias_blends_fng_and_is_point_in_time():
    hist = {"BTC": _series([float(i) for i in range(1, 60)])}
    asof = NOW + timedelta(days=70)
    fng_rows = [{"raw": {"kind": "fng", "value": 80}, "indexed_at": (asof - timedelta(days=1)).isoformat()},
                {"raw": {"kind": "fng", "value": 10}, "indexed_at": (asof + timedelta(days=5)).isoformat()}]  # future → ignored
    out = compute_market_bias(hist, fng_rows, now=asof, fng_weight=0.3)
    assert out["BTC"].label == "bullish-momentum" and out["BTC"].bias > 0
    assert out["BTC"].fng == 80                            # used the as-of (greedy) value, not the future fearful one


def test_market_bias_needs_enough_history():
    assert compute_market_bias({"BTC": _series([1.0, 2.0, 3.0])}, now=NOW) == {}


# ---- planner integration ----------------------------------------------------

def _sig(asset, score):
    return Signal(asset=asset, sentiment=score, strength=0.7, score=score,
                  direction="bullish" if score > 0 else "bearish", mentions=3, velocity=1.0,
                  catalysts=[], latest_at=(NOW - timedelta(minutes=5)).isoformat(), sample=["t"])


def test_market_modifier_boosts_aligned_and_damps_opposed():
    bull = {"BTC": MarketBias("BTC", 0.8, "bullish-momentum", 75.0, 70, [])}
    base = plan([_sig("BTC", 0.5)], now=NOW)[0]
    boosted = plan([_sig("BTC", 0.5)], now=NOW, market_bias=bull, market_weight=0.25)[0]
    assert boosted.confidence > base.confidence and "market bullish-momentum" in boosted.rationale

    # a buy against bearish momentum is damped
    bear = {"BTC": MarketBias("BTC", -0.8, "bearish-momentum", 25.0, 20, [])}
    damped = plan([_sig("BTC", 0.5)], now=NOW, market_bias=bear, market_weight=0.25)[0]
    assert damped.confidence < base.confidence
