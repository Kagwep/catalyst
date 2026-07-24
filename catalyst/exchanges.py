"""Centralized-exchange announcement adapters — listing catalysts at the source.

An exchange listing is one of the fattest catalysts, and the primary source is
public: the exchange's own announcement board. Catching it here beats the RSS
mirrors (regenerated on a 5–15 min cache) and, for most observers, beats the
tweet too — the announcement tweet *links to* this page, so the page exists at
or before tweet time. See ``docs`` / the sources discussion for the sequencing.

Two venues, both keyless:

  - **Binance.** The *official* announcement feed is a WebSocket push, which
    doesn't fit an ephemeral cron poller, so we poll the composite CMS REST
    endpoint (``catalogId=48`` = New Cryptocurrency Listing). That endpoint is
    unofficial and — like Bluesky's public AppView — 403s many datacenter IPs
    behind Cloudflare; a 403 just skips the source (``_safe`` in the pipeline),
    it never sinks the batch. On a hosted box expect to need a residential/
    reputable egress for Binance specifically.

  - **Upbit.** ``api-manager.upbit.com/api/v1/announcements`` is public and
    unauthenticated. Korean listings are the most violent pumps in crypto, so
    this is high-value; titles are Korean and carry the ticker in parentheses,
    which the LLM enrichment step reads fine.

Ban-safety: we are NOT in the millisecond listing-snipe arms race — a polite
poll on the pipeline's own cycle cadence is plenty for catalyst lead-time. Each
announcement has a stable id → stable ``uri`` → the posts-table dedup absorbs
re-emits, so we simply pull the recent window (``since_hours``) every cycle and
let dedup do the rest; no baseline file needed (unlike ``hl_events`` listings,
which diff a full set).

Learning path: ``source="exchange"`` is in ``store.NEWS_SOURCES``, so these are
LLM-enriched, feed the signal layer, and land on the score→outcome records.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .models import Author, Post

BINANCE_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query"
UPBIT_URL = "https://api-manager.upbit.com/api/v1/announcements"

# Binance catalog 48 = "New Cryptocurrency Listing".
DEFAULT_BINANCE_CATALOGS = (48,)
DEFAULT_SINCE_HOURS = 24
DEFAULT_MAX = 20

# A browser-like UA is required to get a 200 from the composite CMS endpoint at
# all; the honest Catalyst UA 403s even from residential IPs here. This is the
# unofficial endpoint's reality, documented rather than hidden.
_BROWSER_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
}
_UPBIT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Catalyst/0.1 (+https://github.com/catalyst)",
}


def _cutoff(since_hours: float | None, now: datetime) -> datetime | None:
    return now - timedelta(hours=since_hours) if since_hours else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---- Binance ----------------------------------------------------------------

def binance_posts(catalogs: list[dict], cutoff: datetime | None,
                  *, max: int = DEFAULT_MAX, now: datetime | None = None) -> list[Post]:
    """[LISTING] posts from Binance CMS catalog blocks, newest window only (pure).

    Each catalog block carries an ``articles`` list of ``{id, code, title,
    releaseDate}`` (releaseDate is a millisecond epoch). Articles older than
    ``cutoff`` are dropped; a missing/unparseable date is kept.
    """
    now = now or datetime.now(timezone.utc)
    out: list[Post] = []
    for cat in catalogs:
        for art in cat.get("articles") or []:
            ts = art.get("releaseDate")
            published = (
                datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                if isinstance(ts, (int, float)) else None
            )
            if cutoff and published and published < cutoff:
                continue
            code = art.get("code") or art.get("id")
            title = (art.get("title") or "").strip()
            iso = (published or now).isoformat()
            out.append(Post(
                source="exchange",
                uri=f"binance:announcement:{art.get('id') or code}",
                url=f"https://www.binance.com/en/support/announcement/{code}" if code else None,
                text=f"[LISTING] Binance: {title}",
                created_at=iso, indexed_at=now.isoformat(),
                author=Author(handle="binance", display_name="Binance Announcements"),
                raw={"kind": "exchange_listing", "venue": "binance",
                     "catalog": cat.get("catalogName"), "title": title},
            ))
            if len(out) >= max:
                return out
    return out


def fetch_binance(*, catalogs=DEFAULT_BINANCE_CATALOGS, since_hours: float | None = DEFAULT_SINCE_HOURS,
                  max: int = DEFAULT_MAX, page_size: int = 20,
                  now: datetime | None = None) -> list[Post]:
    """Recent Binance new-listing announcements. Datacenter 403 → RuntimeError (skipped upstream)."""
    now = now or datetime.now(timezone.utc)
    cutoff = _cutoff(since_hours, now)
    out: list[Post] = []
    for catalog_id in catalogs:
        resp = httpx.get(
            BINANCE_URL,
            params={"catalogId": catalog_id, "pageNo": 1, "pageSize": page_size},
            headers=_BROWSER_HEADERS, timeout=15.0, follow_redirects=True,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"binance announcements failed: {resp.status_code} {resp.reason_phrase}"
                " (composite CMS endpoint often 403s datacenter IPs)"
            )
        blocks = ((resp.json().get("data") or {}).get("catalogs")) or []
        out.extend(binance_posts(blocks, cutoff, max=max - len(out), now=now))
        if len(out) >= max:
            break
    return out[:max]


# ---- Upbit ------------------------------------------------------------------

def upbit_posts(notices: list[dict], cutoff: datetime | None,
                *, max: int = DEFAULT_MAX, now: datetime | None = None) -> list[Post]:
    """[LISTING] posts from Upbit announcement rows, newest window only (pure).

    Rows carry ``{id, title, category, listed_at}``; ``listed_at`` is ISO with a
    KST offset. Older-than-cutoff rows are dropped; unparseable dates are kept.
    """
    now = now or datetime.now(timezone.utc)
    out: list[Post] = []
    for n in notices:
        published = _parse_iso(n.get("listed_at") or n.get("first_listed_at"))
        if cutoff and published and published < cutoff:
            continue
        nid = n.get("id")
        title = (n.get("title") or "").strip()
        out.append(Post(
            source="exchange",
            uri=f"upbit:announcement:{nid}",
            url=f"https://upbit.com/service_center/notice?id={nid}" if nid else None,
            text=f"[LISTING] Upbit: {title}",
            created_at=(published or now).isoformat(), indexed_at=now.isoformat(),
            author=Author(handle="upbit", display_name="Upbit Notices"),
            raw={"kind": "exchange_listing", "venue": "upbit",
                 "category": n.get("category"), "title": title},
        ))
        if len(out) >= max:
            break
    return out


def fetch_upbit(*, category: str = "trade", since_hours: float | None = DEFAULT_SINCE_HOURS,
                max: int = DEFAULT_MAX, per_page: int = 20,
                now: datetime | None = None) -> list[Post]:
    """Recent Upbit listing notices (category ``trade`` = 거래/listings)."""
    now = now or datetime.now(timezone.utc)
    resp = httpx.get(
        UPBIT_URL,
        params={"os": "web", "page": 1, "per_page": per_page, "category": category},
        headers=_UPBIT_HEADERS, timeout=15.0, follow_redirects=True,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"upbit announcements failed: {resp.status_code} {resp.reason_phrase}")
    data = resp.json().get("data") or {}
    notices = data.get("notices") or data.get("list") or []
    return upbit_posts(notices, _cutoff(since_hours, now), max=max, now=now)


def fetch_exchanges(cfg: dict[str, Any], *, now: datetime | None = None) -> list[Post]:
    """Dispatch to the venues named in ``cfg``, each fail-soft."""
    import sys

    now = now or datetime.now(timezone.utc)
    out: list[Post] = []
    # Presence enables a venue; `{}` means on-with-defaults, `false` disables.
    b = cfg.get("binance", False)
    if b is not False:
        bo = b if isinstance(b, dict) else {}
        try:
            out += fetch_binance(
                catalogs=bo.get("catalogs", DEFAULT_BINANCE_CATALOGS),
                since_hours=bo.get("since_hours", DEFAULT_SINCE_HOURS),
                max=bo.get("max", DEFAULT_MAX), now=now,
            )
        except Exception as err:  # noqa: BLE001 — one venue shouldn't sink the rest
            print(f"binance announcements skipped: {err}", file=sys.stderr)
    u = cfg.get("upbit", False)
    if u is not False:
        uo = u if isinstance(u, dict) else {}
        try:
            out += fetch_upbit(
                category=uo.get("category", "trade"),
                since_hours=uo.get("since_hours", DEFAULT_SINCE_HOURS),
                max=uo.get("max", DEFAULT_MAX), now=now,
            )
        except Exception as err:  # noqa: BLE001
            print(f"upbit announcements skipped: {err}", file=sys.stderr)
    return out
