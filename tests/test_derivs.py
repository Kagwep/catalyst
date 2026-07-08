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


def block_binance_and_bybit():
    respx.get(url__startswith=derivs.FUNDING_URL).mock(return_value=httpx.Response(451, text=""))
    respx.get(url__startswith=derivs.OI_HIST_URL).mock(return_value=httpx.Response(451, text=""))
    respx.get(url__startswith=derivs.BYBIT_FUNDING_URL).mock(return_value=httpx.Response(403, text=""))
    respx.get(url__startswith=derivs.BYBIT_TICKERS_URL).mock(return_value=httpx.Response(403, text=""))


@respx.mock
def test_kraken_is_third_and_normalizes_hourly_funding_to_8h():
    block_binance_and_bybit()
    respx.get(url__startswith=derivs.KRAKEN_FUNDING_URL).mock(
        return_value=httpx.Response(200, json={"rates": [
            {"timestamp": "2026-07-01T00:00:00.000Z", "relativeFundingRate": 0.0000125},
            {"timestamp": "2026-07-01T01:00:00.000Z", "relativeFundingRate": 0.000025},
        ]})
    )
    posts = fetch_funding(["BTC"], limit=1)  # limit slices to the newest entry
    assert len(posts) == 1
    assert posts[0].raw["provider"] == "kraken"
    assert posts[0].raw["funding_rate"] == pytest.approx(0.0002)  # 0.000025 × 8
    assert posts[0].text.startswith("[DERIVS] BTCUSDT ")  # canonical label, not PF_XBTUSD
    assert "PF_XBTUSD" in posts[0].url


@respx.mock
def test_hyperliquid_is_last_resort_and_normalizes_to_8h():
    block_binance_and_bybit()
    respx.get(url__startswith=derivs.KRAKEN_FUNDING_URL).mock(
        return_value=httpx.Response(403, text="")
    )
    respx.post(derivs.HYPERLIQUID_INFO_URL).mock(
        return_value=httpx.Response(200, json=[{"coin": "BTC", "fundingRate": "0.0000125",
                                                "time": TS}])
    )
    posts = fetch_funding(["BTC"])
    assert posts[0].raw["provider"] == "hyperliquid"
    assert posts[0].raw["funding_rate"] == pytest.approx(0.0001)  # 0.0000125 × 8
    assert posts[0].uri == f"derivs:funding:BTC:{TS}"


@respx.mock
def test_kraken_oi_is_coin_times_mark():
    block_binance_and_bybit()
    respx.get(url__startswith=derivs.KRAKEN_TICKERS_URL).mock(
        return_value=httpx.Response(200, json={
            "serverTime": "2026-07-01T00:00:00.000Z",
            "tickers": [{"symbol": "PF_XBTUSD", "openInterest": 100.0, "markPrice": 90000.0}],
        })
    )
    posts = fetch_open_interest(["BTC"])
    assert posts[0].raw["provider"] == "kraken"
    assert posts[0].raw["oi_usd"] == pytest.approx(9_000_000.0)


@respx.mock
def test_all_providers_failing_raises_a_combined_error():
    block_binance_and_bybit()
    respx.get(url__startswith=derivs.KRAKEN_FUNDING_URL).mock(
        return_value=httpx.Response(403, text="")
    )
    respx.post(derivs.HYPERLIQUID_INFO_URL).mock(return_value=httpx.Response(500, text=""))
    with pytest.raises(RuntimeError, match="binance.*451") as e:
        fetch_funding(["BTC"])
    for provider in ("bybit", "kraken", "hyperliquid"):
        assert provider in str(e.value)


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
