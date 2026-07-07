from datetime import datetime, timedelta, timezone

from catalyst.signals import compute_signals

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def row(uri, assets, score, *, source="bluesky", handle="x", catalyst=None,
        age_h=1.0, likes=0, reposts=0, text="t"):
    return {
        "uri": uri,
        "source": source,
        "author_handle": handle,
        "indexed_at": (NOW - timedelta(hours=age_h)).isoformat(),
        "text": text,
        "sentiment_score": score,
        "catalyst": catalyst,
        "assets": assets,                # list form (compute_signals also parses JSON)
        "likes": likes,
        "reposts": reposts,
    }


def test_direction_and_ranking():
    rows = [
        row("a", ["BTC"], 0.8, handle="watcher.guru", catalyst="etf"),
        row("b", ["BTC"], 0.6, source="rss"),
        row("c", ["ETH"], -0.9, source="defillama", handle="defillama", catalyst="hack",
            text="HACK: protocol exploited"),
    ]
    sigs = compute_signals(rows, now=NOW, primary_handles=frozenset({"watcher.guru"}))
    by = {s.asset: s for s in sigs}
    assert by["BTC"].direction == "bullish"
    assert by["ETH"].direction == "bearish"
    # Both should be present and ranked by |score|.
    assert [s.asset for s in sigs] == sorted(by, key=lambda a: abs(by[a].score), reverse=True)
    assert by["ETH"].catalysts == ["hack"]


def test_window_excludes_old_posts():
    rows = [
        row("fresh", ["SOL"], 0.7, age_h=2.0),
        row("stale", ["SOL"], -0.7, age_h=48.0),   # outside 24h window
    ]
    sigs = compute_signals(rows, now=NOW, window_hours=24)
    sol = next(s for s in sigs if s.asset == "SOL")
    assert sol.mentions == 1          # stale one dropped
    assert sol.sentiment > 0          # only the fresh bullish post counts


def test_primary_and_catalyst_increase_strength():
    base = [row("p1", ["DOGE"], 0.5, source="rss", handle="someblog")]
    boosted = [row("p2", ["DOGE"], 0.5, handle="watcher.guru", catalyst="etf")]
    s_base = compute_signals(base, now=NOW)[0]
    s_boost = compute_signals(boosted, now=NOW, primary_handles=frozenset({"watcher.guru"}))[0]
    assert s_boost.strength > s_base.strength    # same sentiment, more credible weight


def test_recency_decay_weights_newer_higher():
    rows = [
        row("new", ["XRP"], 1.0, age_h=0.5),
        row("old", ["XRP"], -1.0, age_h=20.0),
    ]
    s = compute_signals(rows, now=NOW, window_hours=24, halflife_hours=6)[0]
    # Newer positive post dominates after time-decay despite equal magnitudes.
    assert s.sentiment > 0


def test_parses_json_assets_and_min_strength_filter():
    import json

    rows = [{**row("j", None, 0.9, catalyst="listing"), "assets": json.dumps(["LINK"])}]
    sigs = compute_signals(rows, now=NOW, min_strength=0.0)
    assert sigs[0].asset == "LINK"
    # A single weak post won't clear a high strength floor.
    assert compute_signals(rows, now=NOW, min_strength=0.99) == []
