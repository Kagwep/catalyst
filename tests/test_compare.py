from datetime import datetime, timedelta, timezone

from catalyst.compare import compare_weights

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _row(asset, score, catalyst):
    return {
        "uri": f"{asset}-{catalyst}",
        "source": "defillama" if catalyst == "hack" else "bluesky",
        "author_handle": "x",
        "indexed_at": (NOW - timedelta(hours=1)).isoformat(),
        "text": "t",
        "sentiment_score": score,
        "catalyst": catalyst,
        "assets": [asset],
        "likes": 0,
        "reposts": 0,
    }


def test_compare_reports_deltas_sorted_by_magnitude():
    rows = [_row("BTC", -0.6, "hack"), _row("ETH", 0.4, None)]
    diffs = compare_weights(
        rows, a=None, b={"catalyst_weights": {"hack": 5.0}},
        now=NOW, window_hours=24, primary_handles=frozenset(),
    )
    # BTC's hack weight was boosted on side B -> its |score| grows the most.
    assert diffs[0]["asset"] == "BTC"
    assert diffs[0]["score_delta"] < 0          # more bearish under B
    assert diffs[0]["strength_b"] > diffs[0]["strength_a"]
    # ETH (no catalyst) is unchanged between A and B.
    eth = next(d for d in diffs if d["asset"] == "ETH")
    assert eth["score_delta"] == 0.0
    # Shape sanity.
    assert {"rank_a", "rank_b", "direction_b", "catalysts"} <= set(diffs[0])


def test_identical_configs_zero_delta():
    rows = [_row("BTC", 0.5, "etf")]
    diffs = compare_weights(rows, a=None, b=None, now=NOW)
    assert diffs[0]["score_delta"] == 0.0
