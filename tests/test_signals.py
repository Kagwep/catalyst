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


# ---- Phase 8a: severity weighting ------------------------------------------

def test_severity_weight_scales_strength():
    high = [{**row("h", ["BTC"], 0.6), "severity": "high"}]
    low = [{**row("l", ["BTC"], 0.6), "severity": "low"}]
    s_high = compute_signals(high, now=NOW)[0]
    s_low = compute_signals(low, now=NOW)[0]
    assert s_high.strength > s_low.strength      # high severity carries more weight


def test_null_severity_degrades_cleanly_and_explicit_none_damps():
    # Lexicon rows (no severity key → NULL) keep weight 1.0; an explicit "none"
    # (LLM saw it, judged it not market-moving) damps hard below that.
    lexicon = [row("lex", ["BTC"], 0.6)]                       # no severity key
    none = [{**row("n", ["BTC"], 0.6), "severity": "none"}]
    s_lex = compute_signals(lexicon, now=NOW)[0]
    s_none = compute_signals(none, now=NOW)[0]
    assert s_none.strength < s_lex.strength


# ---- Phase 8a: story dedup + confirmation bonus -----------------------------

def _news(uri, event, handle, sent=0.7):
    return {**row(uri, ["BTC"], sent, source="rss", handle=handle, catalyst="etf"),
            "event": event}


def test_story_dedup_collapses_syndicated_reposts():
    # One story carried by three outlets must NOT count as three independent votes.
    same = [_news("a", "SEC approves the spot Bitcoin ETF", "outletA"),
            _news("b", "SEC approves spot Bitcoin ETF today", "outletB"),
            _news("c", "SEC approves the spot Bitcoin ETF", "outletC")]
    distinct = [_news("d", "SEC approves the spot Bitcoin ETF", "outletA"),
                _news("e", "Bitcoin miner capitulation deepens sharply", "outletB"),
                _news("f", "Large whale moves fifty thousand coins", "outletC")]
    s_same = compute_signals(same, now=NOW)[0]
    s_distinct = compute_signals(distinct, now=NOW)[0]
    # Three distinct events = three votes → more weighted volume than one deduped story.
    assert s_distinct.strength > s_same.strength
    # But raw mentions stay on post counts (velocity/mentions semantics unchanged).
    assert s_same.mentions == 3


def test_confirmation_bonus_beats_single_source():
    one = [_news("a", "SEC approves the spot Bitcoin ETF", "outletA")]
    three = [_news("a", "SEC approves the spot Bitcoin ETF", "outletA"),
             _news("b", "SEC approves the spot Bitcoin ETF", "outletB"),
             _news("c", "SEC approves the spot Bitcoin ETF", "outletC")]
    s_one = compute_signals(one, now=NOW)[0]
    s_three = compute_signals(three, now=NOW)[0]
    # Same single story, but corroboration from extra distinct sources lifts strength.
    assert s_three.strength > s_one.strength


def test_null_event_posts_are_not_clustered():
    # No `event` → each post votes individually (pre-8a behaviour), even if identical.
    posts = [row("a", ["BTC"], 0.7, catalyst="etf"),
             row("b", ["BTC"], 0.7, catalyst="etf"),
             row("c", ["BTC"], 0.7, catalyst="etf")]
    single = [row("a", ["BTC"], 0.7, catalyst="etf")]
    assert compute_signals(posts, now=NOW)[0].strength > compute_signals(single, now=NOW)[0].strength


# ---- Phase 8a: per-catalyst decay ------------------------------------------

def test_per_catalyst_halflife_controls_decay():
    # A 4h-old hack post: default hack half-life is 2h (fast fade); a longer
    # override half-life fades it less → higher surviving weight → higher strength.
    hack = [row("k", ["BTC"], 0.8, catalyst="hack", age_h=4.0)]
    fast = compute_signals(hack, now=NOW)[0]                                   # hack=2h default
    slow = compute_signals(hack, now=NOW, catalyst_halflives={"hack": 24.0})[0]
    assert slow.strength > fast.strength
