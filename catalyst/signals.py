"""Signal layer — aggregate enriched posts into per-asset trade signals.

For each asset (ticker) mentioned across recently enriched posts, combine:
  - directional sentiment   (weighted mean of per-post sentiment_score)
  - weighted volume         (how much *credible* attention it's getting)
  - recency                 (exponential time-decay — short-term reactionary)
  - source weight           (primary handles + DefiLlama boosted)
  - catalyst weight         (hack / etf / listing / liquidation amplify)
  - engagement              (likes/reposts, a mild confidence bump)

Output is a ranked list of Signals. `score = sentiment * strength` is the signed
conviction the planner ranks on; `direction` is its sign with a neutral band.
This is derived analytics over the store — it reads, never writes.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

# Weight by the post's `source` field; unknown sources default to 1.0.
DEFAULT_SOURCE_WEIGHTS = {"defillama": 1.4, "bluesky": 1.0, "rss": 0.9, "github": 1.2}
PRIMARY_BOOST = 1.6  # applied when author_handle is in primary_handles

# Catalysts amplify a post's contribution (price-reactivity proxy).
CATALYST_WEIGHTS = {
    "hack": 2.0, "etf": 1.8, "liquidation": 1.6, "listing": 1.5, "unlock": 1.5,
    "regulation": 1.4, "mainnet": 1.3, "tvl": 1.3, "release": 1.3, "upgrade": 1.3,
    "treasury": 1.3, "timelock": 1.2, "governance": 1.3, "partnership": 1.2,
}
_STRENGTH_SATURATION = 3.0  # weighted-volume scale for the 0..1 strength curve


@dataclass
class Signal:
    asset: str
    sentiment: float        # weighted mean, -1..1
    strength: float         # 0..1 confidence from weighted volume
    score: float            # sentiment * strength (signed conviction)
    direction: str          # "bullish" | "bearish" | "neutral"
    mentions: int
    velocity: float         # recent-half vs prior-half mention ratio
    catalysts: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    latest_at: str | None = None
    sample: list[str] = field(default_factory=list)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _assets(row: dict) -> list[str]:
    a = row.get("assets")
    if isinstance(a, list):
        return a
    if isinstance(a, str) and a:
        try:
            return json.loads(a)
        except json.JSONDecodeError:
            return []
    return []


def load_weights(path: str) -> dict:
    """Load a weights/tuning JSON file (source_weights, catalyst_weights, …)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _source_weight(row: dict, weights: dict, primary: frozenset[str], primary_boost: float) -> float:
    w = weights.get(row.get("source"), 1.0)
    if row.get("author_handle") in primary:
        w *= primary_boost
    return w


def _engagement(row: dict) -> float:
    e = (row.get("likes") or 0) + 2 * (row.get("reposts") or 0)
    return 1.0 + min(0.5, math.log1p(e) / 12.0)


def compute_signals(
    rows: Iterable[dict],
    *,
    now: datetime | None = None,
    window_hours: float = 24.0,
    halflife_hours: float = 6.0,
    source_weights: dict | None = None,
    catalyst_weights: dict | None = None,
    primary_handles: frozenset[str] = frozenset(),
    primary_boost: float = PRIMARY_BOOST,
    strength_saturation: float = _STRENGTH_SATURATION,
    min_strength: float = 0.0,
) -> list[Signal]:
    """Aggregate enriched rows into ranked per-asset Signals (by |score|).

    All weighting knobs (source/catalyst weights, primary boost, strength
    saturation) are overridable for tuning — see `load_weights`.
    """
    now = now or datetime.now(timezone.utc)
    weights = {**DEFAULT_SOURCE_WEIGHTS, **(source_weights or {})}
    cat_weights = {**CATALYST_WEIGHTS, **(catalyst_weights or {})}
    cutoff = now - timedelta(hours=window_hours)
    midpoint = now - timedelta(hours=window_hours / 2)

    # Per-asset accumulators.
    acc: dict[str, dict] = {}
    for row in rows:
        score = row.get("sentiment_score")
        if score is None:
            continue
        dt = _parse_dt(row.get("indexed_at"))
        if dt is None or dt < cutoff:
            continue

        age_h = max(0.0, (now - dt).total_seconds() / 3600.0)
        decay = 0.5 ** (age_h / halflife_hours)
        w = (
            decay
            * _source_weight(row, weights, primary_handles, primary_boost)
            * cat_weights.get(row.get("catalyst"), 1.0)
            * _engagement(row)
        )

        for asset in _assets(row):
            a = acc.setdefault(
                asset,
                {"wsum": 0.0, "wscore": 0.0, "n": 0, "recent": 0, "cats": set(),
                 "srcs": {}, "latest": None, "contrib": []},
            )
            a["wsum"] += w
            a["wscore"] += w * float(score)
            a["n"] += 1
            if dt >= midpoint:
                a["recent"] += 1
            if row.get("catalyst"):
                a["cats"].add(row["catalyst"])
            handle = row.get("author_handle") or row.get("source") or "?"
            a["srcs"][handle] = a["srcs"].get(handle, 0) + 1
            if a["latest"] is None or dt > a["latest"]:
                a["latest"] = dt
            a["contrib"].append((w, row.get("text") or ""))

    signals: list[Signal] = []
    for asset, a in acc.items():
        if a["wsum"] <= 0:
            continue
        sentiment = max(-1.0, min(1.0, a["wscore"] / a["wsum"]))
        strength = 1.0 - math.exp(-a["wsum"] / strength_saturation)
        score = sentiment * strength
        direction = "bullish" if sentiment > 0.1 else "bearish" if sentiment < -0.1 else "neutral"
        prior = a["n"] - a["recent"]
        velocity = a["recent"] / prior if prior else float(a["recent"])
        top = [t for _, t in sorted(a["contrib"], key=lambda x: x[0], reverse=True)[:3]]

        if strength < min_strength:
            continue
        signals.append(
            Signal(
                asset=asset,
                sentiment=round(sentiment, 3),
                strength=round(strength, 3),
                score=round(score, 3),
                direction=direction,
                mentions=a["n"],
                velocity=round(velocity, 2),
                catalysts=sorted(a["cats"]),
                sources=[h for h, _ in sorted(a["srcs"].items(), key=lambda x: x[1], reverse=True)],
                latest_at=a["latest"].isoformat() if a["latest"] else None,
                sample=[t[:90] for t in top],
            )
        )

    signals.sort(key=lambda s: abs(s.score), reverse=True)
    return signals
