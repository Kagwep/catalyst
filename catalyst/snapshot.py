"""Snapshot governance adapter — DAO proposals via the Snapshot GraphQL API.

Emits the normalized Post shape (source="snapshot"). When a `symbols` map ties a
space to its token ticker, the ticker is embedded in the text so the proposal
attributes to a tradeable asset and the enrichment classifies it as the
`governance` catalyst. Free API, no key.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from .models import Author, Post

ENDPOINT = "https://hub.snapshot.org/graphql"
_USER_AGENT = "Catalyst/0.1 (+https://github.com/catalyst)"
_QUERY = """
query Proposals($first: Int!, $where: ProposalWhere) {
  proposals(first: $first, where: $where, orderBy: "created", orderDirection: desc) {
    id
    title
    state
    created
    author
    link
    space { id name }
  }
}
"""


def _iso(unix: Any) -> str | None:
    if not unix:
        return None
    return datetime.fromtimestamp(int(unix), tz=timezone.utc).isoformat()


def _proposal_to_post(p: dict, symbols: dict | None) -> Post:
    space = p.get("space") or {}
    space_id = space.get("id")
    space_name = space.get("name") or space_id or "?"
    sym = (symbols or {}).get(space_id)
    ticker = f" ${sym}" if sym else ""
    pid = p.get("id")
    return Post(
        source="snapshot",
        uri=f"snapshot:proposal:{pid}",
        url=p.get("link") or (f"https://snapshot.org/#/{space_id}/proposal/{pid}" if pid else None),
        text=f"Governance proposal{ticker} [{space_name}]: {p.get('title', '')} ({p.get('state')})",
        created_at=_iso(p.get("created")),
        indexed_at=_iso(p.get("created")),
        author=Author(handle=sym or space_id, display_name=space_name),
        raw=p,
    )


def proposals_to_posts(
    raw: list[dict], *, symbols: dict | None = None, max: int | None = None
) -> list[Post]:
    posts = [_proposal_to_post(p, symbols) for p in raw]
    return posts[:max] if max else posts


def fetch_proposals(
    spaces: Iterable[str],
    *,
    state: str | None = "active",
    first: int = 20,
    max: int | None = None,
    symbols: dict | None = None,
) -> list[Post]:
    """Recent proposals for the given Snapshot spaces, newest-first.

    `state` is one of "active" / "closed" / "pending", or None/"all" for any.
    """
    where: dict[str, Any] = {"space_in": list(spaces)}
    if state and state != "all":
        where["state"] = state
    resp = httpx.post(
        ENDPOINT,
        json={"query": _QUERY, "variables": {"first": first, "where": where}},
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Snapshot GraphQL failed: {resp.status_code} {resp.reason_phrase}")
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"Snapshot GraphQL errors: {data['errors']}")
    props = (data.get("data") or {}).get("proposals") or []
    return proposals_to_posts(props, symbols=symbols, max=max)
