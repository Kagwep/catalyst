import httpx
import pytest
import respx

from catalyst import bluesky

APPVIEW = bluesky.PUBLIC_APPVIEW
PDS = bluesky.PDS_URL


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    """Keep tests on the public path regardless of the host env; reset session cache."""
    monkeypatch.delenv("BLUESKY_HANDLE", raising=False)
    monkeypatch.delenv("BLUESKY_APP_PASSWORD", raising=False)
    bluesky._auth_client = None


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


def set_credentials(monkeypatch):
    monkeypatch.setenv("BLUESKY_HANDLE", "oracle.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "xxxx-xxxx-xxxx-xxxx")


def test_leading_at_is_stripped_from_the_handle(monkeypatch):
    monkeypatch.setenv("BLUESKY_HANDLE", "@oracle.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "xxxx-xxxx-xxxx-xxxx")
    assert bluesky._credentials() == ("oracle.bsky.social", "xxxx-xxxx-xxxx-xxxx")


@respx.mock
def test_with_credentials_logs_in_and_searches_via_pds(monkeypatch):
    set_credentials(monkeypatch)
    login = respx.post(f"{PDS}/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(200, json={"accessJwt": "jwt1", "refreshJwt": "r1"})
    )
    search = respx.get(url__startswith=f"{PDS}/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(200, json=page("rkey1", "a.com", 3, None))
    )
    out = bluesky.search_posts("news", max=1)
    assert login.call_count == 1
    assert search.calls[0].request.headers["Authorization"] == "Bearer jwt1"
    assert out[0].uri == "rkey1"

    # Session is cached: a second search must not log in again.
    bluesky.search_posts("news", max=1)
    assert login.call_count == 1


@respx.mock
def test_expired_token_triggers_relogin_and_retry(monkeypatch):
    set_credentials(monkeypatch)
    login = respx.post(f"{PDS}/xrpc/com.atproto.server.createSession")
    login.side_effect = [
        httpx.Response(200, json={"accessJwt": "jwt1", "refreshJwt": "r1"}),
        httpx.Response(200, json={"accessJwt": "jwt2", "refreshJwt": "r2"}),
    ]
    search = respx.get(url__startswith=f"{PDS}/xrpc/app.bsky.feed.searchPosts")
    search.side_effect = [
        httpx.Response(400, json={"error": "ExpiredToken", "message": "Token has expired"}),
        httpx.Response(200, json=page("p1", "a.com", 1, None)),
    ]
    out = bluesky.search_posts("news", max=1)
    assert login.call_count == 2
    assert search.calls[1].request.headers["Authorization"] == "Bearer jwt2"
    assert [p.uri for p in out] == ["p1"]


@respx.mock
def test_bad_credentials_raise_a_clear_login_error(monkeypatch):
    set_credentials(monkeypatch)
    respx.post(f"{PDS}/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(401, json={"error": "AuthenticationRequired"})
    )
    with pytest.raises(RuntimeError, match="login failed"):
        bluesky.search_posts("news", max=1)
