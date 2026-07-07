"""The normalized post model shared by every source adapter.

Using pydantic gives downstream data/ML code schema guarantees and validation,
plus easy JSON (`model_dump(mode="json")`) and dict round-tripping.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field


class Author(BaseModel):
    did: str | None = None
    handle: str | None = None
    display_name: str | None = None


class Metrics(BaseModel):
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    quotes: int = 0


class Post(BaseModel):
    """A source-agnostic post/article record.

    `source` distinguishes adapters ("bluesky", "rss"); `uri` is the dedup key.
    """

    source: str
    uri: str
    cid: str | None = None
    url: str | None = None
    text: str = ""
    created_at: str | None = None
    indexed_at: str | None = None
    author: Author = Field(default_factory=Author)
    metrics: Metrics = Field(default_factory=Metrics)
    raw: dict[str, Any] | None = None

    @staticmethod
    def bsky_web_url(handle: str | None, uri: str | None) -> str | None:
        """Build the human bsky.app URL from a handle + at:// post URI."""
        if not handle or not uri:
            return None
        rkey = uri.rstrip("/").split("/")[-1]
        return f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else None

    def to_row(self) -> dict[str, Any]:
        """Flatten into the column shape used by the SQLite store."""
        return {
            "uri": self.uri,
            "cid": self.cid,
            "source": self.source,
            "url": self.url,
            "text": self.text,
            "created_at": self.created_at,
            "indexed_at": self.indexed_at,
            "author_did": self.author.did,
            "author_handle": self.author.handle,
            "author_name": self.author.display_name,
            "likes": self.metrics.likes,
            "reposts": self.metrics.reposts,
            "replies": self.metrics.replies,
            "quotes": self.metrics.quotes,
            "raw": json.dumps(self.raw) if self.raw is not None else None,
        }
