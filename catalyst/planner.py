"""Planner — turn ranked signals into prioritized candidate actions.

This is the top of the oracle: it reads the per-asset Signals from the signal
layer and proposes actions ({asset, action, confidence, horizon, rationale,
freshness}). It applies thresholds (only act on real conviction), a staleness
gate (reactionary trades must be fresh), and a cooldown (don't re-fire the same
asset/direction within a window).

IMPORTANT: the planner *proposes* only. It never sizes, places, or manages
trades. Position sizing, risk limits, and execution are the operator's
responsibility. Nothing here is financial advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from .signals import Signal

# Catalysts that imply a fast, intraday reaction and lift confidence.
_FAST_CATALYSTS = {"hack", "liquidation", "etf"}
_HIGH_IMPACT = {"hack", "etf", "listing", "liquidation", "unlock"}


@dataclass
class Action:
    asset: str
    action: str              # "buy" (long) | "sell" (short/exit) | "watch"
    direction: str           # bullish | bearish | neutral
    confidence: float        # 0..1
    horizon: str             # "intraday" | "short"
    score: float             # underlying signal conviction
    rationale: str
    catalysts: list[str] = field(default_factory=list)
    freshness_minutes: float | None = None
    created_at: str | None = None
    # Structured per-layer contributions (macro/flow/supply/market/derivs) that
    # moved confidence — the machine-readable twin of `rationale`. Each value:
    # {label, bias, effect: "boost"|"damp", weight}. Consumed by payload.py so an
    # alert/Croo recipient sees exactly which layers pushed which way.
    layers: dict = field(default_factory=dict)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _rationale(
    sig: Signal, act: str, fresh_min: float | None, stale: bool,
    layer_notes: list[str], conflict: bool = False,
) -> str:
    # Watch-signal framing (no buy/sell/hold verb): "<DIRECTION> <ALERT|WATCH> <ASSET>".
    tier = "WATCH" if act == "watch" else "ALERT"
    parts = [
        f"{sig.direction.upper()} {tier} {sig.asset}",
        f"score {sig.score:+.2f}",
        f"sentiment {sig.sentiment:+.2f}",
        f"strength {sig.strength:.2f}",
        f"{sig.mentions} mention(s)",
    ]
    if sig.catalysts:
        parts.append("catalysts: " + ",".join(sig.catalysts))
    if sig.velocity:
        parts.append(f"velocity {sig.velocity}")
    if fresh_min is not None:
        parts.append(f"latest {int(fresh_min)}m ago" + (" — STALE" if stale else ""))
    parts.extend(layer_notes)
    if conflict:
        parts.append("CONFLICT: layers disagree → downgraded to watch")
    return " | ".join(parts)


def plan(
    signals: Iterable[Signal],
    *,
    now: datetime | None = None,
    buy_threshold: float = 0.2,
    watch_threshold: float = 0.1,
    min_confidence: float = 0.0,
    max_age_minutes: float | None = None,
    fast_max_age_minutes: float | None = None,
    recent_actions: Iterable[dict] | None = None,
    cooldown_minutes: float = 120.0,
    cooldown_break_delta: float = 0.15,
    conflict_margin: float = 0.2,
    regime=None,
    macro_weight: float = 0.3,
    flow_bias=None,
    flow_weight: float = 0.25,
    supply_bias=None,
    supply_weight: float = 0.25,
    market_bias=None,
    market_weight: float = 0.25,
    derivs_bias=None,
    derivs_weight: float = 0.25,
    trend_bias=None,
    trend_weight: float = 0.25,
    swing_trend_threshold: float = 0.2,
    swing_max_age_minutes: float | None = None,
) -> list[Action]:
    """Propose actions from ranked signals, sorted by confidence.

    `recent_actions` (dicts with asset/action/created_at) suppress a fresh
    proposal that repeats an asset+action emitted within `cooldown_minutes`.

    `regime` (a MacroRegime) scales confidence by `macro_weight`: buy/sell
    aligned with the risk regime is boosted, against it is damped.

    `flow_bias` (a {asset: FlowBias} map from the flows layer) scales confidence
    by `flow_weight` the same way, but per-asset: a buy with money flowing in is
    boosted, a buy while money flows out is damped — i.e. sentiment/flow
    divergence fades itself.

    `supply_bias` (a {asset: SupplyBias} map from the on-chain tier) scales
    confidence by `supply_weight`, also per-asset: a supply sink (staking lockup)
    boosts buys, supply pressure (an imminent unlock) damps them.

    `market_bias` (a {asset: MarketBias} map from the market layer) scales
    confidence by `market_weight`, per-asset: bullish price momentum (RSI/MACD +
    Fear & Greed) boosts buys, bearish momentum damps them.

    `derivs_bias` (a {asset: DerivsBias} map from the derivatives layer) scales
    confidence by `derivs_weight`, per-asset: crowded positioning (extreme funding
    / OI) fades the aligned trade — the same divergence logic as flows.

    `trend_bias` (a {asset: TrendBias} map from the trend layer) scales confidence
    by `trend_weight`, per-asset: a multi-day *rising* bias (strengthening
    accumulation) boosts an aligned buy, a falling one damps it — direction of
    travel over days, on top of the point-in-time layers. It also sets the
    **horizon**: a persistent trend (`|bias| >= swing_trend_threshold`) on a
    non-fast signal promotes `short → swing` (multi-day), which uses the looser
    `swing_max_age_minutes` staleness gate so a multi-day setup isn't killed by the
    intraday freshness cut.

    Phase-2 gates: intraday catalysts expire faster than short ones
    (`fast_max_age_minutes`); a repeat inside the cooldown still fires if it's
    materially more confident (`cooldown_break_delta`); and when the modifier
    layers on balance oppose the trade (`conflict_margin`) it's downgraded to
    `watch` rather than emitted as a low-confidence buy/sell.
    """
    now = now or datetime.now(timezone.utc)

    # Cooldown lookup: most-recent (created_at, confidence) per (asset, action).
    # Missing confidence → +inf so a repeat can never "break" the cooldown on a
    # record that predates confidence tracking.
    cooldown: dict[tuple[str, str], tuple[datetime, float]] = {}
    for ra in recent_actions or []:
        dt = _parse_dt(ra.get("created_at"))
        if not dt:
            continue
        key = (ra.get("asset"), ra.get("action"))
        conf = float(ra["confidence"]) if ra.get("confidence") is not None else float("inf")
        if key not in cooldown or dt > cooldown[key][0]:
            cooldown[key] = (dt, conf)

    out: list[Action] = []
    for sig in signals:
        score = sig.score
        high_impact = bool(set(sig.catalysts) & _HIGH_IMPACT)

        if score >= buy_threshold:
            act = "buy"
        elif score <= -buy_threshold:
            act = "sell"
        elif abs(score) >= watch_threshold or high_impact:
            act = "watch"
        else:
            continue  # not enough conviction to surface

        # Horizon up front — it selects the staleness threshold. Three tiers:
        # `intraday` (fast, reactionary catalysts — must be freshest), `short`
        # (default), and `swing` (multi-day) when the trend layer shows a
        # *persistent* multi-day move on this asset. Fast always wins (a hack is
        # intraday regardless of the multi-day trend).
        fast = bool(set(sig.catalysts) & _FAST_CATALYSTS) or sig.velocity >= 2.0
        tobj = trend_bias.get(sig.asset) if trend_bias else None
        persistent_trend = tobj is not None and abs(float(getattr(tobj, "bias", 0.0))) >= swing_trend_threshold
        if fast:
            horizon = "intraday"
        elif persistent_trend:
            horizon = "swing"
        else:
            horizon = "short"

        latest = _parse_dt(sig.latest_at)
        fresh_min = (now - latest).total_seconds() / 60.0 if latest else None
        age_limit = (fast_max_age_minutes if (fast and fast_max_age_minutes is not None)
                     else swing_max_age_minutes if (horizon == "swing" and swing_max_age_minutes is not None)
                     else max_age_minutes)
        stale = bool(age_limit and fresh_min is not None and fresh_min > age_limit)
        if stale and act in ("buy", "sell"):
            act = "watch"

        cat_bonus = 0.1 if high_impact else 0.0
        confidence = min(1.0, 0.6 * abs(score) + 0.4 * sig.strength + cat_bonus)

        # Collect the applicable modifiers (macro is market-wide; the rest are
        # per-asset bias maps), then apply them uniformly. Each records a
        # structured `layers` entry + a human note, and contributes to a signed
        # net alignment used for conflict detection.
        modifiers: list[tuple[str, str, float, float]] = []
        if regime is not None and regime.score:
            modifiers.append(("macro", regime.label, float(regime.score), macro_weight))
        for name, bmap, w in (("flow", flow_bias, flow_weight),
                              ("supply", supply_bias, supply_weight),
                              ("market", market_bias, market_weight),
                              ("derivs", derivs_bias, derivs_weight),
                              ("trend", trend_bias, trend_weight)):
            obj = bmap.get(sig.asset) if bmap else None
            if obj is not None and getattr(obj, "bias", 0.0):
                modifiers.append((name, obj.label, float(obj.bias), w))

        layers: dict = {}
        layer_notes: list[str] = []
        net_align = 0.0
        directional = act in ("buy", "sell")
        dir_sign = 1 if act == "buy" else -1     # only meaningful when directional
        for name, label, bias, w in modifiers:
            if directional:
                # Align each layer against the trade and fold it into confidence.
                align = dir_sign * (1 if bias > 0 else -1)
                confidence = max(0.0, min(1.0, confidence * (1 + w * align * abs(bias))))
                net_align += w * align * abs(bias)
                effect = "boost" if align > 0 else "damp"
            else:
                # A watch has no trade direction to align against — surface each
                # layer's own tilt as context (real info, no confidence effect).
                effect = "bullish" if bias > 0 else "bearish"
            layers[name] = {"label": label, "bias": round(bias, 3),
                            "effect": effect, "weight": w}
            layer_notes.append(f"{name} {label} ({bias:+.2f})")

        # Conflict resolution: if the layers on balance push against the trade,
        # it's not a clean setup — surface it as a watch, not a weak buy/sell.
        conflict = False
        if act in ("buy", "sell") and net_align <= -conflict_margin:
            act = "watch"
            conflict = True

        confidence = round(confidence, 3)
        if confidence < min_confidence:
            continue

        # Cooldown: suppress a repeat of the same asset+action inside the window
        # — unless it's materially more confident than the prior one.
        last = cooldown.get((sig.asset, act))
        if last:
            last_dt, last_conf = last
            within = (now - last_dt).total_seconds() / 60.0 < cooldown_minutes
            if within and confidence - last_conf < cooldown_break_delta:
                continue

        out.append(
            Action(
                asset=sig.asset,
                action=act,
                direction=sig.direction,
                confidence=confidence,
                horizon=horizon,
                score=sig.score,
                rationale=_rationale(sig, act, fresh_min, stale, layer_notes, conflict),
                catalysts=list(sig.catalysts),
                freshness_minutes=round(fresh_min, 1) if fresh_min is not None else None,
                created_at=now.isoformat(),
                layers=layers,
            )
        )

    out.sort(key=lambda a: a.confidence, reverse=True)
    return out
