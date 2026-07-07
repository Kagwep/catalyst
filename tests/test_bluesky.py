import httpx
import pytest
import respx

from catalyst import bluesky

APPVIEW = bluesky.PUBLIC_APPVIEW


def page(uri, handle, likes, cursor):
    return {
        "posts": [
            {
                "uri": uri,
                "cid": "cid_" + uri,
                "indexedAt": "2026-06-15T10:00:00Z",
                "likeCount": likes,
                "repostCount": 0,
                "replyCount": 0,
                "quoteCount": 0,
                "author": {"did": "did:plc:" + handle, "handle": handle, "displayName": handle.upper()},
                "record": {"text": "post " + uri, "createdAt": "2026-06-15T09:59:00Z"},
            }
        ],
        "cursor": cursor,
    }


@respx.mock
def test_normalizes_a_post_into_the_flat_shape():
    respx.get(url__startswith=f"{APPVIEW}/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(200, json=page("rkey1", "nytimes.com", 12, None))
    )
    post = bluesky.search_posts("news", max=1)[0]
    assert post.source == "bluesky"
    assert post.uri == "rkey1"
    assert post.url == "https://bsky.app/profile/nytimes.com/post/rkey1"
    assert post.text == "post rkey1"
    assert post.author.handle == "nytimes.com"
    assert post.metrics.likes == 12


@respx.mock
def test_follows_cursor_across_pages_up_to_max():
    route = respx.get(url__startswith=f"{APPVIEW}/xrpc/app.bsky.feed.searchPosts")
    route.side_effect = [
        httpx.Response(200, json=page("p1", "a.com", 1, "next")),
        httpx.Response(200, json=page("p2", "b.com", 2, None)),
    ]
    out = bluesky.search_posts("news", limit=1, max=2)
    assert route.call_count == 2
    assert [p.uri for p in out] == ["p1", "p2"]


@respx.mock
def test_stops_at_max_even_if_more_pages_exist():
    # Always returns a cursor, so only `max` should cap it.
    respx.get(url__startswith=f"{APPVIEW}/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(200, json=page("x", "c.com", 0, "more"))
    )
    out = bluesky.search_posts("news", limit=5, max=5)
    assert len(out) == 5


@respx.mock
def test_raises_helpful_error_on_non_ok():
    respx.get(url__startswith=f"{APPVIEW}/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(429, text="rate limited")
    )
    with pytest.raises(RuntimeError, match="429"):
        bluesky.search_posts("news", max=1)
