"""RSS / Atom adapter — emits the same normalized Post shape as the Bluesky
adapter, so feed items flow through the store, query, and poller unchanged.

Uses feedparser, which robustly handles RSS 2.0, Atom, entities, CDATA, and the
many date formats publishers use — replacing the hand-rolled regex parser.
"""

from __future__ import annotations

from calendar import timegm
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from .models import Author, Post

_USER_AGENT = "Catalyst/0.1 (+https://github.com/catalyst)"


def _struct_to_iso(parsed: Any) -> str | None:
    """Convert a feedparser time.struct_time (UTC) to an ISO 8601 string."""
    if not parsed:
        return None
    return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc).isoformat()


def _entry_to_post(entry: Any, feed_title: str) -> Post:
    created = _struct_to_iso(
        entry.get("published_parsed") or entry.get("updated_parsed")
    )
    # entry.id is the guid (RSS) / id (Atom); fall back to the link.
    uri = entry.get("id") or entry.get("link")

    return Post(
        source="rss",
        uri=uri,
        cid=None,
        url=entry.get("link"),
        text=entry.get("title", ""),
        created_at=created,
        indexed_at=created,  # feeds have no separate "indexed" time
        author=Author(
            did=None,
            handle=feed_title,
            display_name=entry.get("author"),
        ),
        # metrics default to zero — feeds carry no engagement signal
        raw={
            "title": entry.get("title"),
            "link": entry.get("link"),
            "id": entry.get("id"),
            "summary": entry.get("summary"),
        },
    )


def parse_feed(text: str) -> list[Post]:
    """Parse RSS 2.0 or Atom XML into normalized posts."""
    parsed = feedparser.parse(text)
    feed_title = (parsed.feed.get("title") if parsed.feed else None) or "rss"
    posts = [_entry_to_post(e, feed_title) for e in parsed.entries]
    return [p for p in posts if p.uri]  # drop entries with no usable key


def fetch_feed(url: str, *, max: int | None = None) -> list[Post]:
    """Fetch and parse a single RSS/Atom feed."""
    resp = httpx.get(
        url,
        headers={
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
            "User-Agent": _USER_AGENT,
        },
        timeout=15.0,
        follow_redirects=True,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"RSS fetch {url} failed: {resp.status_code} {resp.reason_phrase}"
        )
    items = parse_feed(resp.text)
    return items[:max] if max else items
