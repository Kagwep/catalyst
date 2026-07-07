"""Derivatives layer — perp funding → per-asset positioning bias."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from catalyst import derivs
from catalyst.derivs import DerivsBias, _symbol, compute_derivs_bias, fetch_funding, fetch_open_interest
from catalyst.enrich import extract_assets
from catalyst.models import Post

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)

TS = 1782000000000  # an arbitrary funding timestamp (ms)


@pytest.fixture(autouse=True)
def fresh_provider_state(monkeypatch):
    monkeypatch.delenv("DERIVS_PROVIDER", raising=False)
    derivs._active_provider = None


def binance_funding_route():
    return respx.get(url__startswith=derivs.FUNDING_URL).mock(
        return_value=httpx.Response(200, json=[{"fundingTime": TS, "fundingRate": "0.0002"}])
    )


def bybit_funding_route():
    return respx.get(url__startswith=derivs.BYBIT_FUNDING_URL).mock(
        return_value=httpx.Response(200, json={
            "retCode": 0, "retMsg": "OK",
            "result": {"list": [{"symbol": "BTCUSDT", "fundingRate": "0.0003",
                                 "fundingRateTimestamp": str(TS)}]},
        })
    )


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


@respx.mock
def test_funding_prefers_binance_when_it_answers():
    binance = binance_funding_route()
    bybit = bybit_funding_route()
    posts = fetch_funding(["BTC"])
    assert binance.call_count == 1
    assert bybit.call_count == 0
    assert posts[0].raw["provider"] == "binance"
    assert posts[0].raw["funding_rate"] == 0.0002
    assert posts[0].uri == f"derivs:funding:BTC:{TS}"


@respx.mock
def test_funding_falls_back_to_bybit_and_sticks():
    binance = respx.get(url__startswith=derivs.FUNDING_URL).mock(
        return_value=httpx.Response(451, text="")
    )
    bybit = bybit_funding_route()
    posts = fetch_funding(["BTC"])
    assert posts[0].raw["provider"] == "bybit"
    assert posts[0].raw["funding_rate"] == 0.0003
    assert posts[0].uri == f"derivs:funding:BTC:{TS}"  # provider-agnostic dedupe key

    # Sticky: the next call goes straight to bybit, no doomed binance request.
    fetch_funding(["BTC"])
    assert binance.call_count == 1
    assert bybit.call_count == 2


@respx.mock
def test_forced_provider_skips_the_chain(monkeypatch):
    monkeypatch.setenv("DERIVS_PROVIDER", "bybit")
    binance = binance_funding_route()
    bybit = bybit_funding_route()
    posts = fetch_funding(["BTC"])
    assert binance.call_count == 0
    assert bybit.call_count == 1
    assert posts[0].raw["provider"] == "bybit"


@respx.mock
def test_all_providers_failing_raises_a_combined_error():
    respx.get(url__startswith=derivs.FUNDING_URL).mock(return_value=httpx.Response(451, text=""))
    respx.get(url__startswith=derivs.BYBIT_FUNDING_URL).mock(
        return_value=httpx.Response(200, json={"retCode": 10001, "retMsg": "params error"})
    )
    with pytest.raises(RuntimeError, match="binance.*451") as e:
        fetch_funding(["BTC"])
    assert "bybit" in str(e.value)


@respx.mock
def test_bybit_oi_uses_current_usd_value_from_tickers():
    respx.get(url__startswith=derivs.OI_HIST_URL).mock(return_value=httpx.Response(451, text=""))
    respx.get(url__startswith=derivs.BYBIT_TICKERS_URL).mock(
        return_value=httpx.Response(200, json={
            "retCode": 0, "retMsg": "OK", "time": TS,
            "result": {"list": [{"symbol": "BTCUSDT", "openInterestValue": "9000000000"}]},
        })
    )
    posts = fetch_open_interest(["BTC"])
    assert len(posts) == 1
    assert posts[0].raw == {"kind": "oi", "asset": "BTC", "symbol": "BTCUSDT",
                            "oi_usd": 9e9, "provider": "bybit"}


def test_oi_becomes_a_driver_not_a_bias():
    oi = Post(source="derivs", uri="derivs:oi:BTC:1", text="[DERIVS] BTCUSDT open interest $9,000M",
              indexed_at=NOW.isoformat(), raw={"kind": "oi", "asset": "BTC", "oi_usd": 9e9})
    b = compute_derivs_bias([_funding("BTC", 0.001), oi])["BTC"]
    assert any("OI" in d for d in b.drivers)
    assert isinstance(b, DerivsBias)
