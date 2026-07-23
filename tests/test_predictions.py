import json
from datetime import datetime, timezone

import catalyst.predictions as pm


def _gamma_market(**over):
    m = {
        "id": "111", "question": "Will the Fed cut rates in September?",
        "slug": "fed-cut-september", "active": True, "closed": False,
        "volume24hr": 2_100_000.0,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
        "events": [{"slug": "fed-september"}],
    }
    m.update(over)
    return m


def _hist(*probs, start=1_700_000_000):
    return [{"t": start + i * 3600, "p": p} for i, p in enumerate(probs)]


# ---- Polymarket -------------------------------------------------------------

def test_select_polymarket_filters_terms_volume_and_caps():
    markets = [
        _gamma_market(),
        _gamma_market(id="2", question="Will Team X win the World Cup final?"),
        _gamma_market(id="3", question="Bitcoin above $100k by year end?", volume24hr=100.0),
        _gamma_market(id="4", question="ETH ETF approved this quarter?"),
        _gamma_market(id="5", question="CPI above 3% in August?", closed=True),
    ]
    picked = pm.select_polymarket(markets, min_volume_24h=50_000, max_markets=1)
    assert [m["id"] for m in picked] == ["111"]          # cap applies after filters
    picked = pm.select_polymarket(markets, min_volume_24h=50_000, max_markets=10)
    # sports (no term), low-volume, and closed markets are all dropped
    assert [m["id"] for m in picked] == ["111", "4"]


def test_polymarket_shift_post_emits_on_threshold():
    post = pm.polymarket_shift_post(_gamma_market(), _hist(0.30, 0.45, 0.62), min_shift=0.10)
    assert post is not None
    assert post.source == "predictions"
    assert post.uri.startswith("polymarket:111:")
    assert "30% -> 62%" in post.text and "+32pp/24h" in post.text
    assert "[PREDICTION]" in post.text and "Fed cut" in post.text
    assert post.raw["platform"] == "polymarket"
    assert abs(post.raw["shift_24h"] - 0.32) < 1e-9
    assert post.url == "https://polymarket.com/event/fed-september"


def test_polymarket_shift_post_quiet_market_is_none():
    assert pm.polymarket_shift_post(_gamma_market(), _hist(0.50, 0.52), min_shift=0.10) is None
    assert pm.polymarket_shift_post(_gamma_market(), _hist(0.50), min_shift=0.10) is None
    assert pm.polymarket_shift_post(_gamma_market(), [], min_shift=0.10) is None


def test_fetch_polymarket_end_to_end(monkeypatch):
    calls = []

    def fake_get(url, params, **kw):
        calls.append(url)
        if url == pm.GAMMA_MARKETS_URL:
            return [_gamma_market()]
        return {"history": _hist(0.30, 0.62)}

    monkeypatch.setattr(pm, "_get", fake_get)
    posts = pm.fetch_polymarket()
    assert len(posts) == 1 and posts[0].raw["prob"] == 0.62
    assert calls == [pm.GAMMA_MARKETS_URL, pm.CLOB_HISTORY_URL]


# ---- Kalshi -----------------------------------------------------------------

def _kalshi_market(**over):
    m = {
        "ticker": "KXFEDDECISION-26SEP-C25", "event_ticker": "KXFEDDECISION-26SEP",
        "title": "Will the Fed cut rates by 25bps in September?",
        "last_price": 62, "previous_price": 34, "volume_24h": 4200,
    }
    m.update(over)
    return m


def test_kalshi_shift_posts_emits_and_filters():
    now = datetime(2026, 7, 23, tzinfo=timezone.utc)
    markets = [
        _kalshi_market(),
        _kalshi_market(ticker="T2", last_price=None),            # far-dated, no trade
        _kalshi_market(ticker="T3", volume_24h=3),               # illiquid
        _kalshi_market(ticker="T4", last_price=52, previous_price=50),  # quiet
    ]
    posts = pm.kalshi_shift_posts(markets, min_shift=0.10, min_volume_24h=500, now=now)
    assert len(posts) == 1
    p = posts[0]
    assert p.uri == "kalshi:KXFEDDECISION-26SEP-C25:2026-07-23"
    assert "34% -> 62%" in p.text and "+28pp/24h" in p.text
    assert p.raw["platform"] == "kalshi" and p.raw["prob"] == 0.62
    assert p.url == "https://kalshi.com/markets/kxfeddecision-26sep"


def test_kalshi_shift_posts_reads_dollar_string_fields():
    # The elections API serves *_dollars strings with null int-cent fields.
    m = _kalshi_market(last_price=None, previous_price=None, volume_24h=None,
                       last_price_dollars="0.6200", previous_price_dollars="0.3400",
                       volume_24h_fp="4200.00")
    posts = pm.kalshi_shift_posts([m], min_shift=0.10, min_volume_24h=500)
    assert len(posts) == 1
    assert "34% -> 62%" in posts[0].text and posts[0].raw["prob"] == 0.62


def test_fetch_predictions_isolates_platform_failures(monkeypatch):
    def boom(**kw):
        raise RuntimeError("gamma down")

    monkeypatch.setattr(pm, "fetch_polymarket", boom)
    monkeypatch.setattr(pm, "fetch_kalshi",
                        lambda **kw: pm.kalshi_shift_posts([_kalshi_market()]))
    posts = pm.fetch_predictions(polymarket={}, kalshi={})
    assert len(posts) == 1 and posts[0].raw["platform"] == "kalshi"
