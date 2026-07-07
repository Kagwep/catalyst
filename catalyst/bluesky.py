"""Bluesky (AT Protocol) adapter — public read endpoints, optional auth.

By default requests hit the public AppView keyless. That works from residential
IPs, but the public AppView blocks most datacenter/cloud IPs with an HTML 403,
so on a hosted box set ``BLUESKY_HANDLE`` + ``BLUESKY_APP_PASSWORD`` (an app
password from Settings → Privacy and Security → App Passwords): the adapter
then creates a session on the PDS and makes authenticated XRPC calls, which are
not IP-blocked. Sessions are re-created transparently when the access token
expires (~2h; well inside createSession rate limits for a poller).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .models import Author, Metrics, Post

PUBLIC_APPVIEW = "https://public.api.bsky.app"
PDS_URL = os.environ.get("BLUESKY_PDS_URL", "https://bsky.social")
_USER_AGENT = "Catalyst/0.1 (+https://github.com/catalyst)"
_HEADERS = {"Accept": "application/json", "User-Agent": _USER_AGENT}

# Reused across calls. Some CDN-fronted endpoints 403 requests with no UA.
_client = httpx.Client(base_url=PUBLIC_APPVIEW, headers=_HEADERS, timeout=15.0)

_auth_client: httpx.Client | None = None


def _credentials() -> tuple[str, str] | None:
    handle = os.environ.get("BLUESKY_HANDLE")
    password = os.environ.get("BLUESKY_APP_PASSWORD")
    return (handle, password) if handle and password else None


def _login() -> httpx.Client:
    """Create (or re-create) an authenticated session on the PDS."""
    global _auth_client
    handle, password = _credentials()  # only called when credentials exist
    resp = httpx.post(
        f"{PDS_URL}/xrpc/com.atproto.server.createSession",
        json={"identifier": handle, "password": password},
        headers=_HEADERS,
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Bluesky login failed for {handle}: {resp.status_code} {resp.reason_phrase}"
            f" — {resp.text[:300]}"
        )
    token = resp.json()["accessJwt"]
    if _auth_client is not None:
        _auth_client.close()
    _auth_client = httpx.Client(
        base_url=PDS_URL,
        headers={**_HEADERS, "Authorization": f"Bearer {token}"},
        timeout=15.0,
    )
    return _auth_client


def xrpc_get(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Low-level XRPC GET — authenticated PDS when credentials are set, else public AppView."""
    clean = {k: v for k, v in params.items() if v is not None}
    authed = _credentials() is not None
    client = (_auth_client or _login()) if authed else _client
    resp = client.get(f"/xrpc/{method}", params=clean)
    if authed and resp.status_code in (400, 401) and "ExpiredToken" in resp.text:
        resp = _login().get(f"/xrpc/{method}", params=clean)
    if resp.status_code != 200:
        body = resp.text[:500]
        raise RuntimeError(
            f"Bluesky {method} failed: {resp.status_code} {resp.reason_phrase}"
            + (f" — {body}" if body else "")
        )
    return resp.json()


def _normalize(raw: dict[str, Any]) -> Post:
    """Normalize a raw feed/search item into a Post.

    Handles both searchPosts (item is the post) and getAuthorFeed (post is
    nested under item["post"]).
    """
    post = raw.get("post", raw)
    record = post.get("record", {}) or {}
    author = post.get("author", {}) or {}
    handle = author.get("handle")

    return Post(
        source="bluesky",
        uri=post.get("uri"),
        cid=post.get("cid"),
        url=Post.bsky_web_url(handle, post.get("uri")),
        text=record.get("text", ""),
        created_at=record.get("createdAt"),
        indexed_at=post.get("indexedAt"),
        author=Author(
            did=author.get("did"),
            handle=handle,
            display_name=author.get("displayName"),
        ),
        metrics=Metrics(
            likes=post.get("likeCount", 0),
            reposts=post.get("repostCount", 0),
            replies=post.get("replyCount", 0),
            quotes=post.get("quoteCount", 0),
        ),
        raw=post,
    )


def _collect(method: str, base_params: dict[str, Any], limit: int, max_items: int) -> list[Post]:
    """Follow the `cursor` field until `max_items` are collected or the feed ends."""
    out: list[Post] = []
    cursor: str | None = None

    while len(out) < max_items:
        page_size = min(limit, max_items - len(out))
        data = xrpc_get(method, {**base_params, "limit": page_size, "cursor": cursor})
        items = data.get("posts") or data.get("feed") or []
        out.extend(_normalize(it) for it in items)

        cursor = data.get("cursor")
        if not cursor or not items:
            break

    return out[:max_items]


def search_posts(
    query: str,
    *,
    limit: int = 25,
    max: int = 25,
    sort: str | None = None,
    since: str | None = None,
) -> list[Post]:
    """Search posts across all of Bluesky by keyword/query."""
    return _collect(
        "app.bsky.feed.searchPosts",
        {"q": query, "sort": sort, "since": since},
        limit,
        max,
    )


def get_author_feed(
    actor: str,
    *,
    limit: int = 50,
    max: int = 50,
    filter: str | None = None,
) -> list[Post]:
    """Get an account's posts + reposts (handle or DID)."""
    return _collect(
        "app.bsky.feed.getAuthorFeed",
        {"actor": actor, "filter": filter},
        limit,
        max,
    )
