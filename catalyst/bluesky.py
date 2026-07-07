"""Bluesky (AT Protocol) adapter — public read endpoints, no auth required.

All requests hit the public AppView. These endpoints need no API key, token, or
auth. Auth is only needed for writing, your personal timeline, or the firehose
— none of which this module touches.
"""

from __future__ import annotations

from typing import Any

import httpx

from .models import Author, Metrics, Post

PUBLIC_APPVIEW = "https://public.api.bsky.app"
_USER_AGENT = "Catalyst/0.1 (+https://github.com/catalyst)"

# Reused across calls. Some CDN-fronted endpoints 403 requests with no UA.
_client = httpx.Client(
    base_url=PUBLIC_APPVIEW,
    headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
    timeout=15.0,
)


def xrpc_get(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Low-level XRPC GET against the public AppView."""
    clean = {k: v for k, v in params.items() if v is not None}
    resp = _client.get(f"/xrpc/{method}", params=clean)
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
