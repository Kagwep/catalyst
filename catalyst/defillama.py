"""DefiLlama adapter — protocol risk & ecosystem signals.

Emits the same normalized Post shape as every other source (source="defillama"),
so hacks, TVL moves, and new listings flow through the store, enrichment, and
query layers unchanged. DefiLlama's API is free and needs no key.

Three signal types, in rough order of price-reactivity:
  - hacks      exploits / thefts (the single most price-reactive crypto catalyst)
  - tvl        significant 1d/7d TVL surges or drains on sizable protocols
  - listings   newly listed protocols

Text is phrased so the lexicon enrichment classifies it correctly (a hack post
contains "exploited", a listing contains "listed", TVL uses surges/plunges).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from .models import Author, Post

_BASE = "https://api.llama.fi"
_USER_AGENT = "Catalyst/0.1 (+https://github.com/catalyst)"
_WINDOW_FIELD = {"1h": "change_1h", "1d": "change_1d", "7d": "change_7d"}


def _get(path: str) -> Any:
    resp = httpx.get(
        _BASE + path,
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        timeout=60.0,
        follow_redirects=True,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"DefiLlama {path} failed: {resp.status_code} {resp.reason_phrase}")
    return resp.json()


def _iso(unix: Any) -> str | None:
    if not unix:
        return None
    return datetime.fromtimestamp(int(unix), tz=timezone.utc).isoformat()


def _usd(n: Any) -> str:
    n = float(n or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"${n / div:.1f}{unit}"
    return f"${n:.0f}"


def _chain_str(chain: Any) -> str:
    if isinstance(chain, list):
        return ", ".join(str(c) for c in chain)
    return str(chain) if chain else ""


# ---- Hacks ------------------------------------------------------------------

def _hack_to_post(h: dict) -> Post:
    name = h.get("name") or "Unknown"
    technique = h.get("technique") or h.get("classification") or "exploit"
    chain = _chain_str(h.get("chain"))
    date = h.get("date")
    ident = h.get("defillamaId")
    uri = f"defillama:hack:{ident if ident is not None else name}:{date}"
    text = f"HACK: {name} exploited for {_usd(h.get('amount'))} — {technique}"
    if chain:
        text += f" ({chain})"
    return Post(
        source="defillama",
        uri=uri,
        url=(h.get("source") or None),
        text=text,
        created_at=_iso(date),
        indexed_at=_iso(date),
        author=Author(handle="defillama", display_name=name),
        raw=h,
    )


def hacks_to_posts(
    raw: list[dict], *, since_unix: float | None = None, min_amount: float = 0, max: int | None = None
) -> list[Post]:
    items = [
        h
        for h in raw
        if (h.get("amount") or 0) >= min_amount
        and (since_unix is None or (h.get("date") or 0) >= since_unix)
    ]
    items.sort(key=lambda h: h.get("date") or 0, reverse=True)
    posts = [_hack_to_post(h) for h in items]
    return posts[:max] if max else posts


def fetch_hacks(
    *, since_days: int | None = None, min_amount: float = 0, max: int | None = None
) -> list[Post]:
    """Recent hacks/exploits above `min_amount` (USD), newest-first."""
    since_unix = None
    if since_days:
        since_unix = datetime.now(timezone.utc).timestamp() - since_days * 86400
    return hacks_to_posts(_get("/hacks"), since_unix=since_unix, min_amount=min_amount, max=max)


# ---- Protocols: TVL changes & new listings ----------------------------------

def fetch_protocols() -> list[dict]:
    """The full protocols list (~7k entries). Fetch once, feed both helpers below."""
    return _get("/protocols")


def _tvl_post(p: dict, change: float, window: str, now: datetime) -> Post:
    name = p.get("name") or "Unknown"
    slug = p.get("slug") or name
    move = "surges" if change > 0 else "plunges"
    text = (
        f"{name} TVL {move} {change:+.1f}% ({window}) to {_usd(p.get('tvl'))}"
        f" — {p.get('category') or ''} on {p.get('chain') or ''}"
    ).strip()
    return Post(
        source="defillama",
        # one record per protocol/day/window so re-polls dedupe
        uri=f"defillama:tvl:{slug}:{now.date().isoformat()}:{window}",
        url=f"https://defillama.com/protocol/{slug}",
        text=text,
        created_at=now.isoformat(),
        indexed_at=now.isoformat(),
        author=Author(handle="defillama", display_name=name),
        raw=p,
    )


def tvl_changes(
    protocols: list[dict],
    *,
    min_tvl: float = 50_000_000,
    min_change_pct: float = 15.0,
    window: str = "1d",
    max: int | None = None,
) -> list[Post]:
    """Protocols with |TVL change| over the threshold, ranked by magnitude."""
    field = _WINDOW_FIELD.get(window, "change_1d")
    now = datetime.now(timezone.utc)
    scored: list[tuple[float, Post]] = []
    for p in protocols:
        tvl = p.get("tvl") or 0
        change = p.get(field)
        if tvl < min_tvl or change is None or abs(change) < min_change_pct:
            continue
        scored.append((abs(change), _tvl_post(p, change, window, now)))
    scored.sort(key=lambda t: t[0], reverse=True)
    posts = [p for _, p in scored]
    return posts[:max] if max else posts


def _listing_post(p: dict) -> Post:
    name = p.get("name") or "Unknown"
    slug = p.get("slug") or name
    text = (
        f"New protocol listed: {name} ({_usd(p.get('tvl'))} TVL)"
        f" — {p.get('category') or ''} on {p.get('chain') or ''}"
    ).strip()
    return Post(
        source="defillama",
        uri=f"defillama:listing:{slug}",
        url=f"https://defillama.com/protocol/{slug}",
        text=text,
        created_at=_iso(p.get("listedAt")),
        indexed_at=_iso(p.get("listedAt")),
        author=Author(handle="defillama", display_name=name),
        raw=p,
    )


def new_listings(
    protocols: list[dict], *, days: int = 7, min_tvl: float = 0, max: int | None = None
) -> list[Post]:
    """Protocols listed on DefiLlama within the last `days`, newest-first."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    items = [
        p
        for p in protocols
        if (p.get("listedAt") or 0) >= cutoff and (p.get("tvl") or 0) >= min_tvl
    ]
    items.sort(key=lambda p: p.get("listedAt") or 0, reverse=True)
    posts = [_listing_post(p) for p in items]
    return posts[:max] if max else posts
