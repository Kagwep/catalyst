"""Derivatives layer — perp funding → per-asset positioning bias."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from catalyst.derivs import DerivsBias, _symbol, compute_derivs_bias
from catalyst.enrich import extract_assets
from catalyst.models import Post

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _funding(asset, rate, *, hours_ago=0.0):
    ts = (NOW - timedelta(hours=hours_ago)).isoformat()
    return Post(source="derivs", uri=f"derivs:funding:{asset}:{hours_ago}",
                text=f"[DERIVS] {_symbol(asset)} perp funding {rate*100:+.4f}% (8h)",
                indexed_at=ts, created_at=ts,
                raw={"kind": "funding", "asset": asset, "funding_rate": rate})


def test_symbol_mapping():
    assert _symbol("BTC") == "BTCUSDT"
    assert _symbol("wif") == "WIFUSDT"  # fallback


def test_derivs_text_never_leaks_into_signal_layer():
    # The exchange symbol (BTCUSDT) must NOT be extracted as a $BTC asset.
    p = _funding("BTC", 0.001)
    assert extract_assets(p.text) == []


def test_positive_funding_is_crowded_long_bearish():
    # Persistent positive funding = crowded longs = fade → negative bias.
    b = compute_derivs_bias([_funding("BTC", 0.002, hours_ago=1),
                             _funding("BTC", 0.0015, hours_ago=9)])["BTC"]
    assert b.bias < 0
    assert b.label == "crowded-long"


def test_negative_funding_is_crowded_short_bullish():
    b = compute_derivs_bias([_funding("ETH", -0.002), _funding("ETH", -0.0018, hours_ago=8)])["ETH"]
    assert b.bias > 0
    assert b.label == "crowded-short"


def test_neutral_funding_is_neutral():
    b = compute_derivs_bias([_funding("SOL", 0.00001)])["SOL"]
    assert b.label == "neutral"


def test_recency_decay_weights_latest_more():
    # A fresh big-positive funding should dominate a stale small-negative one.
    rows = [_funding("BTC", 0.003, hours_ago=0.5), _funding("BTC", -0.0005, hours_ago=100)]
    assert compute_derivs_bias(rows)["BTC"].bias < 0


def test_oi_becomes_a_driver_not_a_bias():
    oi = Post(source="derivs", uri="derivs:oi:BTC:1", text="[DERIVS] BTCUSDT open interest $9,000M",
              indexed_at=NOW.isoformat(), raw={"kind": "oi", "asset": "BTC", "oi_usd": 9e9})
    b = compute_derivs_bias([_funding("BTC", 0.001), oi])["BTC"]
    assert any("OI" in d for d in b.drivers)
    assert isinstance(b, DerivsBias)
