"""catalyst.events feed — the breadth service over stored event/severity fields."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from catalyst.croo_agent import events_pipeline
from catalyst.enrich import Enrichment
from catalyst.models import Author, Metrics, Post
from catalyst.payload import build_events_delivery, event_line
from catalyst.store import open_store, save_enrichments, save_posts


def _post(uri, text, hours_ago, source="bluesky"):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Post(source=source, uri=uri, text=text, indexed_at=ts,
                author=Author(handle="x"), metrics=Metrics())


def _en(score, assets, catalyst, event, severity):
    return Enrichment(sentiment_score=score, sentiment_label="x", assets=assets,
                      catalyst=catalyst, model="claude-sonnet-5", event=event, severity=severity)


def _seed(tmp_path):
    db = str(tmp_path / "e.db")
    conn = open_store(db)
    try:
        save_posts(conn, [
            _post("macro", "big macro news", 1),
            _post("btc", "$BTC etf approved", 3),
            _post("lo", "minor filing", 2),
            _post("old", "stale but big", 100),
            _post("noevent", "just chatter", 1),
        ])
        save_enrichments(conn, [
            ("macro", _en(-0.5, [], "macro", "Fed hikes rates hard", "high")),   # asset-less → MARKET
            ("btc", _en(0.6, ["BTC"], "etf", "Spot BTC ETF approved", "high")),
            ("lo", _en(0.05, [], "regulation", "minor SEC filing", "low")),
            ("old", _en(-0.4, [], "macro", "ancient crash", "high")),            # outside window
            ("noevent", _en(0.0, ["DOGE"], None, None, "none")),                 # no event → excluded
        ])
    finally:
        conn.close()
    return db


def test_events_pipeline_filters_ranks_and_marks_market(tmp_path):
    out = events_pipeline(_seed(tmp_path), {})   # default min_severity=medium, window 24h
    assert out["schema"] == "catalyst.events"
    assert out["count"] == 2                      # low + stale + no-event all dropped
    lines = out["events"]
    # both high; most recent first → macro (1h) leads btc (3h)
    assert lines[0].startswith("MARKET | macro | Fed hikes rates hard | bearish | high | ")
    assert any(l.startswith("BTC | etf | Spot BTC ETF approved | bullish | high | ") for l in lines)
    assert out["lead"]["asset"] == "MARKET" and out["lead"]["severity"] == "high"
    assert not any("minor SEC" in l for l in lines)   # low filtered
    assert not any("ancient" in l for l in lines)     # stale filtered
    assert set(out["assets"]) == {"MARKET", "BTC"}


def test_events_min_severity_and_asset_filter(tmp_path):
    db = _seed(tmp_path)
    # lowering the bar pulls the low-severity event in
    low = events_pipeline(db, {"min_severity": "low"})
    assert any("minor SEC" in l for l in low["events"])
    # filtering to a ticker drops the asset-less MARKET events
    btc = events_pipeline(db, {"assets": "BTC"})
    assert btc["count"] == 1 and btc["lead"]["asset"] == "BTC"
    # a wider window admits the 100h-old event
    wide = events_pipeline(db, {"window": "1w"})
    assert any("ancient" in l for l in wide["events"])


def test_build_events_delivery_empty_sentinel():
    out = build_events_delivery([])
    assert out["count"] == 0
    assert out["events"] == ["No notable catalyst events in the current window."]
    assert "lead" not in out and "assets" not in out


def test_event_line_shape():
    line = event_line({"asset": "BTC", "catalyst": "etf", "event": "X approved",
                       "direction": "bullish", "severity": "high", "age": "5m ago"})
    assert line == "BTC | etf | X approved | bullish | high | 5m ago"
