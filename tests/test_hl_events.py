import json
from datetime import datetime, timezone

import catalyst.hl_events as hl

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


# ---- Listings ---------------------------------------------------------------

def test_listing_posts_only_new_coins():
    universe = [{"name": "BTC", "maxLeverage": 40}, {"name": "NEWCOIN", "maxLeverage": 10}]
    ctxs = [{"dayNtlVlm": "1000000"}, {"dayNtlVlm": "2500000"}]
    posts = hl.listing_posts(universe, ctxs, baseline={"BTC"}, now=NOW)
    assert len(posts) == 1
    p = posts[0]
    assert p.uri == "hyperliquid:listing:NEWCOIN"
    assert p.source == "hyperliquid"
    assert "[LISTING] Hyperliquid lists NEWCOIN perpetual" in p.text
    assert "10x leverage" in p.text and "$2.5M day volume" in p.text
    assert p.raw == {"kind": "listing", "asset": "NEWCOIN", "max_leverage": 10,
                     "day_volume_usd": 2500000.0}


def test_fetch_listings_seed_mode_emits_nothing(tmp_path, monkeypatch, capsys):
    # No baseline file → never flood the feed; the network isn't even called.
    monkeypatch.setattr(hl, "_post", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert hl.fetch_listings(baseline_file=str(tmp_path / "missing.json")) == []
    assert "no baseline" in capsys.readouterr().err


def test_baseline_roundtrip(tmp_path, monkeypatch):
    meta = {"universe": [{"name": "eth"}, {"name": "BTC"}]}
    monkeypatch.setattr(hl, "_post", lambda payload, **k: (meta, []))
    path = tmp_path / "u.json"
    assert hl.write_baseline(str(path)) == 2
    assert hl.load_baseline(str(path)) == {"BTC", "ETH"}
    assert json.loads(path.read_text())["universe"] == ["BTC", "ETH"]


# ---- Funding flips ----------------------------------------------------------

def _rows(prev_rate: float, recent_rate: float):
    """48h of hourly rows: first 24h at prev_rate, last 24h at recent_rate."""
    base = int(NOW.timestamp() * 1000) - 48 * 3_600_000
    return [{"time": base + h * 3_600_000,
             "fundingRate": prev_rate if h < 24 else recent_rate}
            for h in range(48)]


def test_flip_post_negative_flip_reads_contrarian_bullish():
    p = hl.flip_post("btc", _rows(0.00002, -0.00003), min_mean=0.0001, now=NOW)
    assert p is not None
    assert p.uri == "hyperliquid:flip:BTC:2026-07-23"
    assert "flipped negative" in p.text and "contrarian bullish" in p.text
    assert p.raw["kind"] == "funding_flip"
    assert p.raw["mean_recent"] < 0 < p.raw["mean_prev"]


def test_flip_post_positive_flip_reads_contrarian_bearish():
    p = hl.flip_post("ETH", _rows(-0.00002, 0.00003), min_mean=0.0001, now=NOW)
    assert p is not None and "contrarian bearish" in p.text


def test_flip_post_none_without_flip_or_below_min():
    assert hl.flip_post("BTC", _rows(0.00002, 0.00003), now=NOW) is None   # same sign
    assert hl.flip_post("BTC", _rows(0.00002, -0.000001), min_mean=0.0001, now=NOW) is None
    assert hl.flip_post("BTC", [], now=NOW) is None                        # no data


def test_fetch_funding_flips_per_asset_fail_soft(monkeypatch):
    def fake_post(payload, **kw):
        if payload.get("coin") == "BTC":
            raise RuntimeError("rate limited")
        return _rows(0.00002, -0.00003)

    monkeypatch.setattr(hl, "_post", fake_post)
    posts = hl.fetch_funding_flips(["BTC", "ETH"], min_mean=0.0001, now=NOW)
    assert [p.raw["asset"] for p in posts] == ["ETH"]
