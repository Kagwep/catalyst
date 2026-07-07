"""Protocol registry — one source of truth tying each protocol to its GitHub
repos, Snapshot space, DeFiLlama slug, and token symbol.

Expands the registry into normalized Posts:
  - GitHub release feeds  → source="github", relabeled "Release: <name> $SYM …"
  - Snapshot proposals    → source="snapshot", attributed to $SYM

Embedding the token symbol means a release or proposal attributes to a tradeable
asset and the enrichment classifies the catalyst (`release` / `governance`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import rss, snapshot
from .models import Author, Post


def load_registry(path: str) -> list[dict]:
    """Read protocols.json. Accepts either a top-level list or {"protocols": [...]}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    protocols = data.get("protocols", []) if isinstance(data, dict) else data
    return [p for p in protocols if isinstance(p, dict)]


def _relabel_release(post: Post, name: str, symbol: str | None) -> Post:
    ticker = f" ${symbol}" if symbol else ""
    return Post(
        source="github",
        uri=post.uri,
        url=post.url,
        text=f"Release: {name}{ticker} — {post.text}",
        created_at=post.created_at,
        indexed_at=post.indexed_at,
        author=Author(handle=symbol or name, display_name=name),
        raw=post.raw,
    )


def fetch_releases(registry: list[dict], *, per_repo_max: int = 5) -> list[Post]:
    """GitHub release feeds for every repo in the registry (via the RSS adapter)."""
    out: list[Post] = []
    for proto in registry:
        name = proto.get("name") or "?"
        symbol = proto.get("symbol")
        for repo in proto.get("github", []) or []:
            url = f"https://github.com/{repo}/releases.atom"
            try:
                for post in rss.fetch_feed(url, max=per_repo_max):
                    out.append(_relabel_release(post, name, symbol))
            except Exception as err:  # noqa: BLE001 — one bad repo shouldn't fail the batch
                print(f"releases {repo} skipped: {err}", file=sys.stderr)
    return out


def fetch_governance(
    registry: list[dict], *, state: str | None = "active", first: int = 20, max: int | None = None
) -> list[Post]:
    """Snapshot proposals for every space in the registry, attributed to symbols."""
    spaces: list[str] = []
    symbols: dict[str, str] = {}
    for proto in registry:
        space = proto.get("snapshot")
        if space:
            spaces.append(space)
            if proto.get("symbol"):
                symbols[space] = proto["symbol"]
    if not spaces:
        return []
    return snapshot.fetch_proposals(spaces, state=state, first=first, max=max, symbols=symbols)
