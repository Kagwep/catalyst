import httpx
import respx

from catalyst import defillama as dl
from catalyst.enrich import LexiconScorer

HACKS = [
    {"name": "Ronin Bridge", "date": 1648000000, "amount": 624000000,
     "technique": "Private Key", "chain": ["Ronin"], "defillamaId": 1, "source": "https://x/r"},
    {"name": "Tiny Exploit", "date": 1700000000, "amount": 50000,
     "technique": "Reentrancy", "chain": ["Ethereum"], "defillamaId": 2, "source": ""},
    {"name": "Recent Big", "date": 1780000000, "amount": 200000000,
     "technique": "Oracle", "chain": ["Solana", "Ethereum"], "defillamaId": 3},
]

PROTOCOLS = [
    {"name": "BigMover", "slug": "bigmover", "tvl": 800_000_000, "change_1d": 22.5,
     "change_7d": 4.0, "category": "Lending", "chain": "Ethereum", "listedAt": 1000},
    {"name": "TinyMover", "slug": "tiny", "tvl": 1_000_000, "change_1d": 80.0,
     "category": "Dex", "chain": "Base", "listedAt": 1000},   # below min_tvl
    {"name": "Stable", "slug": "stable", "tvl": 900_000_000, "change_1d": 0.3,
     "category": "CDP", "chain": "Ethereum", "listedAt": 1000},  # below min_change
    {"name": "Drainer", "slug": "drainer", "tvl": 300_000_000, "change_1d": -35.0,
     "category": "Yield", "chain": "Arbitrum", "listedAt": 1000},
]


def test_hacks_filter_sort_and_shape():
    posts = dl.hacks_to_posts(HACKS, min_amount=1_000_000)
    # Tiny Exploit ($50k) filtered out; newest-first by date
    assert [p.author.display_name for p in posts] == ["Recent Big", "Ronin Bridge"]
    p = posts[1]
    assert p.source == "defillama"
    assert p.uri == "defillama:hack:1:1648000000"
    assert "exploited for $624.0M" in p.text
    assert p.created_at.startswith("2022-")


def test_hack_text_classifies_as_hack_catalyst():
    post = dl.hacks_to_posts(HACKS, min_amount=1_000_000)[0]
    e = LexiconScorer().score(post.text)
    assert e.catalyst == "hack"
    assert e.sentiment_label == "negative"


def test_tvl_changes_threshold_and_ranking():
    posts = dl.tvl_changes(PROTOCOLS, min_tvl=50_000_000, min_change_pct=15, window="1d")
    # TinyMover (small tvl) and Stable (small change) excluded; ranked by magnitude
    assert [p.author.display_name for p in posts] == ["Drainer", "BigMover"]
    assert "plunges -35.0%" in posts[0].text
    assert "surges +22.5%" in posts[1].text
    assert posts[1].uri.startswith("defillama:tvl:bigmover:")


def test_new_listings_window_and_min_tvl():
    import time

    now = int(time.time())
    protocols = [
        {"name": "FreshBig", "slug": "fresh", "tvl": 5_000_000, "listedAt": now - 86400,
         "category": "Dex", "chain": "Ethereum"},
        {"name": "FreshTiny", "slug": "ftiny", "tvl": 1000, "listedAt": now - 86400},  # below min_tvl
        {"name": "Old", "slug": "old", "tvl": 5_000_000, "listedAt": now - 90 * 86400},  # too old
    ]
    posts = dl.new_listings(protocols, days=7, min_tvl=1_000_000)
    assert [p.author.display_name for p in posts] == ["FreshBig"]
    assert "New protocol listed" in posts[0].text


@respx.mock
def test_fetch_hacks_hits_endpoint():
    respx.get("https://api.llama.fi/hacks").mock(return_value=httpx.Response(200, json=HACKS))
    posts = dl.fetch_hacks(min_amount=1_000_000, max=1)
    assert len(posts) == 1
    assert posts[0].author.display_name == "Recent Big"
