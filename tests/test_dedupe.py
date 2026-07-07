"""Cross-source de-dupe — collapse the same story to the highest-trust source."""

from __future__ import annotations

from catalyst.dedupe import _canon_url, collapse_dupes, trust_of
from catalyst.models import Author, Metrics, Post


def _p(source, uri, text, *, url=None, handle=None, likes=0):
    return Post(source=source, uri=uri, text=text, url=url,
                author=Author(handle=handle), metrics=Metrics(likes=likes),
                indexed_at="2026-06-15T10:00:00Z")


def test_canon_url_normalizes():
    assert _canon_url("https://www.Example.com/Story/?utm=x#frag") == "example.com/story"
    assert _canon_url("http://example.com/a/") == "example.com/a"
    assert _canon_url(None) is None


def test_collapse_by_canonical_url_keeps_highest_trust():
    # Same article via a low-trust RSS row and a high-trust DefiLlama row.
    posts = [
        _p("rss", "r1", "Totally different wording here about a thing",
           url="https://site.com/article?utm=1"),
        _p("defillama", "d1", "Another phrasing entirely of the same link",
           url="https://www.site.com/article"),
    ]
    deduped, n = collapse_dupes(posts)
    assert n == 1
    assert len(deduped) == 1
    assert deduped[0].source == "defillama"  # higher DEFAULT_SOURCE_WEIGHTS


def test_collapse_by_title_jaccard():
    posts = [
        _p("bluesky", "b1", "Bitcoin ETF approved by regulators sending price higher"),
        _p("rss", "r1", "regulators approved Bitcoin ETF sending price higher today"),
        _p("bluesky", "b2", "Solana network outage halts block production suddenly"),
    ]
    deduped, n = collapse_dupes(posts)
    assert n == 1  # the two ETF stories collapse; Solana is distinct
    kept = {p.uri for p in deduped}
    assert "b2" in kept and len(kept) == 2


def test_distinct_short_titles_not_merged():
    # Too few significant tokens → never near-dup on tokens alone.
    posts = [_p("rss", "r1", "BTC up"), _p("rss", "r2", "ETH up")]
    _, n = collapse_dupes(posts)
    assert n == 0


def test_primary_handle_boosts_trust():
    primary = frozenset({"watcher.guru"})
    a = _p("bluesky", "a", "x", handle="watcher.guru")
    b = _p("bluesky", "b", "x", handle="rando")
    assert trust_of(a, primary_handles=primary) > trust_of(b, primary_handles=primary)


def test_primary_survives_collapse():
    posts = [
        _p("bluesky", "rando", "Ethereum Dencun upgrade goes live on mainnet successfully",
           handle="rando"),
        _p("bluesky", "primary", "Ethereum Dencun upgrade goes live on mainnet successfully",
           handle="watcher.guru"),
    ]
    deduped, n = collapse_dupes(posts, primary_handles=frozenset({"watcher.guru"}))
    assert n == 1
    assert deduped[0].uri == "primary"
