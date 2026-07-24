from datetime import datetime, timezone

import httpx
import respx

import catalyst.exchanges as ex

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# ---- Binance ----------------------------------------------------------------

def test_binance_posts_builds_listing_and_url():
    fresh = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)
    catalogs = [{
        "catalogName": "New Cryptocurrency Listing",
        "articles": [{"id": 5001, "code": "abc123", "title": "Binance Will List Foo (FOO)",
                      "releaseDate": _ms(fresh)}],
    }]
    posts = ex.binance_posts(catalogs, cutoff=None, now=NOW)
    assert len(posts) == 1
    p = posts[0]
    assert p.uri == "binance:announcement:5001"
    assert p.source == "exchange"
    assert p.url == "https://www.binance.com/en/support/announcement/abc123"
    assert "[LISTING] Binance: Binance Will List Foo (FOO)" == p.text
    assert p.created_at == fresh.isoformat()
    assert p.raw == {"kind": "exchange_listing", "venue": "binance",
                     "catalog": "New Cryptocurrency Listing", "title": "Binance Will List Foo (FOO)"}


def test_binance_posts_drops_stale_outside_window():
    old = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)   # 4+ days back
    new = datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)
    catalogs = [{"catalogName": "L", "articles": [
        {"id": 1, "code": "a", "title": "old", "releaseDate": _ms(old)},
        {"id": 2, "code": "b", "title": "new", "releaseDate": _ms(new)},
    ]}]
    cutoff = NOW.replace(hour=0)  # midnight today → 24h window
    posts = ex.binance_posts(catalogs, cutoff=cutoff, now=NOW)
    assert [p.raw["title"] for p in posts] == ["new"]


def test_binance_posts_respects_max():
    catalogs = [{"catalogName": "L", "articles": [
        {"id": i, "code": str(i), "title": f"t{i}", "releaseDate": _ms(NOW)} for i in range(10)
    ]}]
    assert len(ex.binance_posts(catalogs, cutoff=None, max=3, now=NOW)) == 3


@respx.mock
def test_fetch_binance_parses_envelope():
    body = {"data": {"catalogs": [{"catalogName": "New Cryptocurrency Listing",
            "articles": [{"id": 9, "code": "z", "title": "List Bar (BAR)",
                          "releaseDate": _ms(NOW)}]}]}}
    respx.get(ex.BINANCE_URL).mock(return_value=httpx.Response(200, json=body))
    posts = ex.fetch_binance(since_hours=24, now=NOW)
    assert [p.uri for p in posts] == ["binance:announcement:9"]


@respx.mock
def test_fetch_binance_403_raises():
    respx.get(ex.BINANCE_URL).mock(return_value=httpx.Response(403, text="cloudflare"))
    try:
        ex.fetch_binance(now=NOW)
        assert False, "expected RuntimeError on 403"
    except RuntimeError as err:
        assert "403" in str(err)


# ---- Upbit ------------------------------------------------------------------

def test_upbit_posts_builds_and_filters():
    notices = [
        {"id": 100, "title": "[신규 거래] 폴리곤(MATIC)", "category": "거래",
         "listed_at": "2026-07-24T09:00:00+09:00"},
        {"id": 99, "title": "old notice", "category": "거래",
         "listed_at": "2026-07-01T09:00:00+09:00"},
    ]
    cutoff = datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc)
    posts = ex.upbit_posts(notices, cutoff=cutoff, now=NOW)
    assert len(posts) == 1
    p = posts[0]
    assert p.uri == "upbit:announcement:100"
    assert p.url == "https://upbit.com/service_center/notice?id=100"
    assert p.text.startswith("[LISTING] Upbit: ")
    assert p.raw["venue"] == "upbit"


@respx.mock
def test_fetch_upbit_parses_notices_container():
    body = {"success": True, "data": {"notices": [
        {"id": 7, "title": "List Baz (BAZ)", "category": "거래",
         "listed_at": "2026-07-24T10:00:00+09:00"}]}}
    respx.get(ex.UPBIT_URL).mock(return_value=httpx.Response(200, json=body))
    posts = ex.fetch_upbit(since_hours=48, now=NOW)
    assert [p.uri for p in posts] == ["upbit:announcement:7"]


# ---- Dispatch ---------------------------------------------------------------

def test_fetch_exchanges_one_venue_failure_is_soft(monkeypatch, capsys):
    def boom(**kw):
        raise RuntimeError("403 datacenter")

    monkeypatch.setattr(ex, "fetch_binance", boom)
    monkeypatch.setattr(ex, "fetch_upbit",
                        lambda **kw: ex.upbit_posts(
                            [{"id": 1, "title": "ok", "category": "거래",
                              "listed_at": NOW.isoformat()}], cutoff=None, now=NOW))
    out = ex.fetch_exchanges({"binance": {}, "upbit": {}}, now=NOW)
    assert [p.uri for p in out] == ["upbit:announcement:1"]
    assert "binance announcements skipped" in capsys.readouterr().err
