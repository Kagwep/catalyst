"""Macro layer — interest rates & inflation as a market-wide risk regime.

Rates/inflation news doesn't map to one ticker, so this layer is modeled
differently from the per-asset signal: central-bank announcements (and optional
FRED numeric series) are ingested as `source="macro"` posts, enriched like
everything else, then aggregated into a single **risk regime** score
(risk-on / risk-off). The planner uses that regime to scale confidence —
boosting trades aligned with the regime and damping those against it.

Direction convention: easing / rate cuts / cooling inflation = risk-on
(positive); hiking / tightening / hot inflation = risk-off (negative).
Central-bank RSS works with no key; FRED needs a free key.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from . import rss
from .models import Author, Post

# Central-bank press feeds — consumed via the RSS adapter (no key).
CENTRAL_BANK_FEEDS = {
    "fed": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "ecb": "https://www.ecb.europa.eu/rss/press.html",
}

# A few FRED series; lower value = easing = risk-on.
FRED_SERIES = {
    "FEDFUNDS": "US Fed Funds Rate",
    "ECBDFR": "ECB Deposit Facility Rate",
    "CPIAUCSL": "US CPI",
}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _relabel(post: Post, bank: str) -> Post:
    return Post(
        source="macro",
        uri=post.uri,
        url=post.url,
        text=f"[{bank.upper()}] {post.text}",
        created_at=post.created_at,
        indexed_at=post.indexed_at,
        author=Author(handle=bank, display_name=bank.upper()),
        raw=post.raw,
    )


def fetch_central_banks(feeds: dict | None = None, *, max: int = 10) -> list[Post]:
    """Central-bank press releases (Fed, ECB, …) as macro posts."""
    feeds = feeds or CENTRAL_BANK_FEEDS
    out: list[Post] = []
    for bank, url in feeds.items():
        try:
            for post in rss.fetch_feed(url, max=max):
                out.append(_relabel(post, bank))
        except Exception as err:  # noqa: BLE001 — one feed shouldn't fail the batch
            print(f"central bank {bank} skipped: {err}", file=sys.stderr)
    return out


def _fred_post(sid: str, label: str, obs: dict, prev: dict | None) -> Post:
    val = float(obs["value"])
    pv = float(prev["value"]) if prev else None
    chg = (val - pv) if pv is not None else 0.0
    # Falling rate/inflation = easing = risk-on; rising = tightening = risk-off.
    move = "eases" if chg < 0 else "tightens" if chg > 0 else "holds"
    return Post(
        source="macro",
        uri=f"macro:fred:{sid}:{obs['date']}",
        url=f"https://fred.stlouisfed.org/series/{sid}",
        text=f"[FRED] {label} {move}: {val} ({chg:+.2f} from {pv})",
        created_at=obs["date"],
        indexed_at=obs["date"] + "T00:00:00+00:00",
        author=Author(handle="fred", display_name=label),
        raw=obs,
    )


def fetch_fred(
    series: dict | None = None, *, api_key: str | None = None, max: int | None = None,
    history: int = 1,
) -> list[Post]:
    """FRED series (rates, CPI) as dated macro posts. Requires a free key.

    `history` = how many recent observations to emit per series (each vs its
    prior). history=1 is the latest only; raise it to backfill point-in-time
    history so the macro regime is replayable in a backtest.
    """
    api_key = api_key or os.environ.get("FRED_API_KEY")
    if not api_key:
        raise RuntimeError("FRED requires api_key or FRED_API_KEY env var")
    series = series or FRED_SERIES
    out: list[Post] = []
    for sid, label in series.items():
        data = httpx.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": sid, "api_key": api_key, "file_type": "json",
                    "sort_order": "desc", "limit": history + 1},
            timeout=30.0,
        ).json()
        # newest-first from the API → flip to oldest-first so each obs has its prior
        obs = [o for o in (data.get("observations") or []) if o.get("value") not in (None, ".")]
        obs.reverse()
        posts = [_fred_post(sid, label, obs[i], obs[i - 1] if i else None) for i in range(len(obs))]
        out.extend(posts[-history:])  # the `history` most-recent observations
    return out[:max] if max else out


@dataclass
class MacroRegime:
    score: float                       # -1 risk-off .. +1 risk-on
    label: str                         # risk-on | neutral | risk-off
    evidence: float                    # weighted volume behind the score
    drivers: list[str] = field(default_factory=list)


def _is_macro(row: dict) -> bool:
    return row.get("catalyst") == "macro" or row.get("source") == "macro"


def compute_regime(
    rows: list[dict], *, now: datetime | None = None, window_hours: float = 72.0,
    halflife_hours: float = 24.0,
) -> MacroRegime:
    """Aggregate enriched macro posts into a single risk-regime score."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    wsum = 0.0
    wscore = 0.0
    contrib: list[tuple[float, str]] = []
    for row in rows:
        score = row.get("sentiment_score")
        if score is None or not _is_macro(row):
            continue
        dt = _parse_dt(row.get("indexed_at"))
        if dt is None or dt < cutoff:
            continue
        decay = 0.5 ** ((now - dt).total_seconds() / 3600.0 / halflife_hours)
        wsum += decay
        wscore += decay * float(score)
        contrib.append((decay, row.get("text") or ""))

    if wsum <= 0:
        return MacroRegime(0.0, "neutral", 0.0, [])
    score = max(-1.0, min(1.0, wscore / wsum))
    label = "risk-on" if score > 0.15 else "risk-off" if score < -0.15 else "neutral"
    drivers = [t[:90] for _, t in sorted(contrib, key=lambda x: x[0], reverse=True)[:3]]
    return MacroRegime(round(score, 3), label, round(wsum, 3), drivers)
