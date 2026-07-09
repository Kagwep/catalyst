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
import re
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

# Phase-8a — severity weighting. The LLM enrichment writes a `severity`
# (high|medium|low|none) direct market-moving judgment; multiply the post's
# contribution by it. Lexicon-scored rows have `severity` NULL → weight 1.0, so
# the pre-8a path degrades cleanly and unchanged. An explicit "none" (the LLM
# saw the event and judged it not market-moving) is *not* NULL — it damps hard.
DEFAULT_SEVERITY_WEIGHTS = {"high": 2.0, "medium": 1.2, "low": 0.7, "none": 0.3}

# Phase-8a — per-catalyst time-decay half-lives (hours). A hack reprices in
# hours (short half-life → old posts fade fast); regulation plays out over days
# (long half-life → stays relevant). Anything unlisted falls back to the single
# `halflife_hours` arg (default 6), so this only sharpens the catalysts we've
# reasoned about and leaves everything else exactly as before.
DEFAULT_CATALYST_HALFLIVES = {
    "hack": 2.0, "liquidation": 2.0, "listing": 4.0, "etf": 12.0,
    "regulation": 24.0, "macro": 12.0, "partnership": 8.0, "mainnet": 8.0,
}

# Phase-8a — story dedup. Posts sharing (asset, catalyst, similar `event` text)
# within the window are ONE story, not N independent votes (10 outlets covering
# one ETF approval must not score as 10 events). Two events cluster when their
# token-Jaccard clears this threshold; extra distinct sources add a log-scale
# *confirmation* bonus instead of linear volume.
_DEDUP_THRESHOLD = 0.6
_CONFIRMATION_BONUS = 0.3

# The compute_signals weighting knobs a weights.json / tuner file may override.
SIGNAL_WEIGHT_KEYS = (
    "source_weights", "catalyst_weights", "primary_boost", "strength_saturation",
    "severity_weights", "catalyst_halflives",
)


def signal_kwargs_from_weights(weights: dict | None) -> dict:
    """Pick the compute_signals weighting overrides out of a weights/tuning dict.

    Shared by the CLI (`--weights`), the Croo serving path, and the tuner so the
    override surface is defined in exactly one place."""
    if not weights:
        return {}
    return {k: weights[k] for k in SIGNAL_WEIGHT_KEYS if k in weights}


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


def _severity_weight(row: dict, sev_weights: dict) -> float:
    """Multiplier from the LLM's `severity` judgment; NULL (lexicon rows) → 1.0."""
    sev = row.get("severity")
    if sev is None:          # never enriched by the LLM — leave the weight untouched
        return 1.0
    return sev_weights.get(sev, 1.0)


def _event_tokens(text: str | None) -> frozenset[str]:
    """Normalized token set of an `event` string for cheap similarity clustering."""
    if not text:
        return frozenset()
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cluster_stories(
    posts: list[dict], *, threshold: float, confirmation_bonus: float,
) -> list[tuple[float, float]]:
    """Collapse an asset's per-post contributions into stories → [(weight, sentiment)].

    Posts carrying an `event` are greedily clustered within their `catalyst` by
    token-Jaccard (seeds are the highest-weight posts, so a strong original anchors
    its syndication). Posts with NULL `event` are never clustered — each stays its
    own singleton, contributing exactly as it did pre-8a. Each story reduces to the
    **representative (highest) weight × a confirmation bonus** for extra distinct
    sources, carrying the story's **weight-averaged sentiment** — so N reposts of
    one story vote once (with a log-scale corroboration lift), not N times.
    """
    clusters: list[list[dict]] = []
    seeds: list[tuple[str | None, frozenset[str]]] = []
    # Strongest first so the seed of each cluster is its most-credible post.
    for p in sorted((p for p in posts if p["event"]), key=lambda p: p["w"], reverse=True):
        for i, (scat, stok) in enumerate(seeds):
            if scat == p["catalyst"] and _jaccard(stok, p["tokens"]) >= threshold:
                clusters[i].append(p)
                break
        else:
            clusters.append([p])
            seeds.append((p["catalyst"], p["tokens"]))
    for p in posts:                       # NULL-event posts: one story each
        if not p["event"]:
            clusters.append([p])

    out: list[tuple[float, float]] = []
    for cl in clusters:
        tot_w = sum(pp["w"] for pp in cl)
        if tot_w <= 0:
            continue
        rep_w = max(pp["w"] for pp in cl)
        sentiment = sum(pp["w"] * pp["score"] for pp in cl) / tot_w
        n_extra = len({pp["src"] for pp in cl}) - 1      # distinct corroborating sources
        weight = rep_w * (1.0 + confirmation_bonus * math.log1p(max(0, n_extra)))
        out.append((weight, sentiment))
    return out


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
    severity_weights: dict | None = None,
    catalyst_halflives: dict | None = None,
    confirmation_bonus: float = _CONFIRMATION_BONUS,
    dedup_threshold: float = _DEDUP_THRESHOLD,
    min_strength: float = 0.0,
) -> list[Signal]:
    """Aggregate enriched rows into ranked per-asset Signals (by |score|).

    All weighting knobs (source/catalyst/severity weights, primary boost, strength
    saturation, per-catalyst half-lives) are overridable for tuning — see
    `signal_kwargs_from_weights`.

    Phase-8a additions (all degrade to the pre-8a math when the new fields are
    absent — i.e. lexicon rows with NULL severity/event behave exactly as before):
      - **severity weighting** — each post scaled by its LLM severity judgment.
      - **per-catalyst decay** — a per-catalyst half-life map (falls back to
        `halflife_hours`) so fast catalysts fade faster than slow ones.
      - **story dedup** — posts sharing (asset, catalyst, similar `event`) collapse
        to one weighted story + a log-scale confirmation bonus, instead of each
        syndicated repost counting as an independent vote. `mentions`/`velocity`
        stay on RAW post counts so those semantics don't shift.
    """
    now = now or datetime.now(timezone.utc)
    weights = {**DEFAULT_SOURCE_WEIGHTS, **(source_weights or {})}
    cat_weights = {**CATALYST_WEIGHTS, **(catalyst_weights or {})}
    sev_weights = {**DEFAULT_SEVERITY_WEIGHTS, **(severity_weights or {})}
    cat_halflives = {**DEFAULT_CATALYST_HALFLIVES, **(catalyst_halflives or {})}
    cutoff = now - timedelta(hours=window_hours)
    midpoint = now - timedelta(hours=window_hours / 2)

    # Per-asset accumulators. `wsum`/`wscore` are computed from *stories* (after
    # dedup) at the end; `posts` holds each raw contribution feeding that dedup.
    acc: dict[str, dict] = {}
    for row in rows:
        score = row.get("sentiment_score")
        if score is None:
            continue
        dt = _parse_dt(row.get("indexed_at"))
        if dt is None or dt < cutoff:
            continue

        age_h = max(0.0, (now - dt).total_seconds() / 3600.0)
        cat = row.get("catalyst")
        halflife = cat_halflives.get(cat, halflife_hours) if cat else halflife_hours
        decay = 0.5 ** (age_h / halflife)
        w = (
            decay
            * _source_weight(row, weights, primary_handles, primary_boost)
            * cat_weights.get(cat, 1.0)
            * _engagement(row)
            * _severity_weight(row, sev_weights)
        )
        event = row.get("event")
        tokens = _event_tokens(event)
        handle = row.get("author_handle") or row.get("source") or "?"

        for asset in _assets(row):
            a = acc.setdefault(
                asset,
                {"n": 0, "recent": 0, "cats": set(), "srcs": {}, "latest": None,
                 "posts": [], "contrib": []},
            )
            a["n"] += 1
            if dt >= midpoint:
                a["recent"] += 1
            if cat:
                a["cats"].add(cat)
            a["srcs"][handle] = a["srcs"].get(handle, 0) + 1
            if a["latest"] is None or dt > a["latest"]:
                a["latest"] = dt
            a["posts"].append({"w": w, "score": float(score), "catalyst": cat,
                               "event": event, "tokens": tokens, "src": handle})
            a["contrib"].append((w, row.get("text") or ""))

    signals: list[Signal] = []
    for asset, a in acc.items():
        # Dedup the raw posts into stories, then aggregate over stories: N reposts
        # of one event become one weighted vote (+ a corroboration bonus).
        stories = _cluster_stories(a["posts"], threshold=dedup_threshold,
                                   confirmation_bonus=confirmation_bonus)
        wsum = sum(w for w, _ in stories)
        wscore = sum(w * s for w, s in stories)
        if wsum <= 0:
            continue
        a["wsum"] = wsum
        sentiment = max(-1.0, min(1.0, wscore / wsum))
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
