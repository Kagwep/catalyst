from datetime import datetime, timedelta, timezone

from catalyst.enrich import LexiconScorer
from catalyst.onchain import (
    SupplyBias,
    _unlock_post,
    _upcoming_unlocks,
    compute_stake_bias,
    compute_supply_bias,
    compute_unlock_bias,
)
from catalyst.planner import plan
from catalyst.signals import Signal

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _emissions(events, supply=1_000_000_000, gecko="arbitrum"):
    return {"gecko_id": gecko, "supplyMetrics": {"adjustedSupply": supply},
            "metadata": {"events": events}}


def _ev(days_ahead, tokens, category="insiders", unlock_type="cliff"):
    ts = int((NOW + timedelta(days=days_ahead)).timestamp())
    return {"timestamp": ts, "noOfTokens": [tokens], "category": category, "unlockType": unlock_type}


# ---- unlock event filtering -------------------------------------------------

def test_upcoming_unlocks_keeps_future_sell_pressure_cliffs_only():
    events = [
        _ev(5, 50_000_000, "insiders", "cliff"),       # keep
        _ev(5, 10_000_000, "farming", "cliff"),        # drop: not sell-pressure
        _ev(5, 10_000_000, "insiders", "linear"),      # drop: linear, not a cliff
        _ev(-3, 50_000_000, "insiders", "cliff"),      # drop: in the past
        _ev(99, 50_000_000, "insiders", "cliff"),      # drop: beyond horizon
    ]
    got = _upcoming_unlocks(_emissions(events), now=NOW, horizon_days=30)
    assert len(got) == 1 and got[0]["tokens"] == 50_000_000
    assert abs(got[0]["pct_float"] - 0.05) < 1e-9


# ---- unlock post is a usable standalone catalyst ----------------------------

def test_unlock_post_classifies_bearish_unlock_catalyst_with_ticker():
    ev = {"event_ts": (NOW + timedelta(days=4)).isoformat(), "days_until": 4,
          "tokens": 56_000_000, "pct_float": 0.032, "category": "insiders"}
    post = _unlock_post("ARB", ev, {"price": 0.09}, NOW)
    e = LexiconScorer().score(post.text)
    assert "ARB" in e.assets            # ticker extracted → attributable signal
    assert e.catalyst == "unlock"       # surfaces as an unlock catalyst
    assert e.sentiment_label == "negative"  # bearish → can become a sell candidate


# ---- bias: forward unlocks, standing staking, merge -------------------------

def _unlock_row(asset, pct, days_ahead):
    return {"raw": {"kind": "unlock", "asset": asset, "pct_float": pct,
                    "event_ts": (NOW + timedelta(days=days_ahead)).isoformat()}}


def test_unlock_bias_is_bearish_and_ramps_toward_the_date():
    near = compute_unlock_bias([_unlock_row("ARB", 0.03, 2)], now=NOW, horizon_days=30)["ARB"][0]
    far = compute_unlock_bias([_unlock_row("ARB", 0.03, 28)], now=NOW, horizon_days=30)["ARB"][0]
    assert near < 0 and far < 0           # both bearish
    assert near < far                     # nearer unlock is MORE bearish (forward ramp)


def test_stake_bias_entry_queue_is_bullish_and_eth_only():
    row = {"raw": {"kind": "stake", "asset": "ETH", "entry_eth": 2_800_000, "exit_eth": 46_000},
           "indexed_at": NOW.isoformat()}
    out = compute_stake_bias([row])
    assert out["ETH"][0] > 0.15 and set(out) == {"ETH"}


def test_compute_supply_bias_merges_and_labels():
    rows = [
        _unlock_row("ARB", 0.04, 3),
        {"raw": {"kind": "stake", "asset": "ETH", "entry_eth": 2_800_000, "exit_eth": 46_000},
         "indexed_at": NOW.isoformat()},
    ]
    out = compute_supply_bias(rows, now=NOW)
    assert out["ARB"].label == "supply-pressure" and out["ARB"].bias < 0
    assert out["ETH"].label == "supply-sink" and out["ETH"].bias > 0
    assert compute_supply_bias([], now=NOW) == {}


# ---- planner integration ----------------------------------------------------

def _sig(asset, score):
    return Signal(asset=asset, sentiment=score, strength=0.7, score=score,
                  direction="bullish" if score > 0 else "bearish", mentions=3, velocity=1.0,
                  catalysts=[], latest_at=(NOW - timedelta(minutes=5)).isoformat(), sample=["t"])


def test_supply_modifier_damps_buy_into_unlock_boosts_buy_into_staking():
    pressure = {"ARB": SupplyBias("ARB", -0.8, "supply-pressure", 0.8, [])}
    base_buy = plan([_sig("ARB", 0.5)], now=NOW)[0]
    damped = plan([_sig("ARB", 0.5)], now=NOW, supply_bias=pressure, supply_weight=0.25)[0]
    assert damped.confidence < base_buy.confidence
    assert "supply supply-pressure" in damped.rationale

    sink = {"ETH": SupplyBias("ETH", 0.8, "supply-sink", 0.8, [])}
    base_eth = plan([_sig("ETH", 0.5)], now=NOW)[0]
    boosted = plan([_sig("ETH", 0.5)], now=NOW, supply_bias=sink, supply_weight=0.25)[0]
    assert boosted.confidence > base_eth.confidence


def test_supply_modifier_is_per_asset():
    pressure = {"ARB": SupplyBias("ARB", -0.8, "supply-pressure", 0.8, [])}
    base = plan([_sig("ETH", 0.5)], now=NOW)[0]
    other = plan([_sig("ETH", 0.5)], now=NOW, supply_bias=pressure, supply_weight=0.25)[0]
    assert other.confidence == base.confidence and "supply" not in other.rationale
