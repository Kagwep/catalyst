"""Canonical deliverable payload — the one JSON shape the oracle emits.

This is the **integration seam**. Everything downstream that hands proposals to
the outside world — the Phase-3 alert sinks (webhook/stdout) and the Phase-5 Croo
provider's `deliver_order` — serialises the planner's `Action[]` through *here*,
so they all emit the identical, versioned, self-describing structure. Wiring a
new delivery channel (Croo included) is then "call `build_payload`, ship the
dict" — no bespoke shaping, no drift between channels.

`select_actions` is the buyer-facing filter: it takes the same `requirements`
shape a Croo order carries (`assets` / `horizon` / `signal` / `direction` /
`min_confidence`) and narrows an `Action[]` to what was asked for — so the Phase-5
service contract is a thin wrapper over this, not new logic.

Framing: the deliverable is a **watch signal**, never a trade instruction. The
planner reasons internally in buy/sell/watch, but the payload exposes only a
non-prescriptive `signal` (alert | watch) plus a market `direction` (bullish |
bearish | neutral). "buy" surfaces as a bullish alert, "sell" as a bearish alert.
The disclaimer is baked into every payload: the oracle surfaces catalyst-driven
signals; it does not size, place, or manage trades.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

SCHEMA = "catalyst.signals"
SCHEMA_EVENTS = "catalyst.events"
SCHEMA_VERSION = "2.0"


def signal_of(action: str) -> str:
    """Map the planner's internal action to the deliverable's watch-signal tier.

    buy/sell → `alert` (a directional call, its side carried by `direction`); every
    other value (`watch`, and the ops-monitoring `ops`) passes through unchanged.
    No trade verb (buy/sell/hold) ever leaves this module."""
    return "alert" if action in ("buy", "sell") else action
DISCLAIMER = (
    "Proposals only — not financial advice. The oracle proposes catalyst-driven "
    "signals; it does not size, place, or manage trades."
)


def action_to_dict(a) -> dict:
    """One `Action` → the canonical per-proposal object.

    Carries everything a recipient needs with no outside context: what to do,
    how sure, over what horizon, why (catalysts + the human `rationale`), and the
    machine-readable `layers` breakdown of which modifiers pushed which way.
    """
    return {
        "asset": a.asset,
        "signal": signal_of(a.action),   # alert | watch — never buy/sell/hold
        "direction": a.direction,
        "confidence": a.confidence,
        "horizon": a.horizon,
        "score": a.score,
        "catalysts": list(a.catalysts or []),
        "freshness_minutes": a.freshness_minutes,
        "layers": dict(getattr(a, "layers", {}) or {}),
        "rationale": a.rationale,
        "created_at": a.created_at,
    }


def build_payload(actions, *, generated_at: str | None = None, meta: dict | None = None) -> dict:
    """Serialise `Action[]` into the versioned, self-describing deliverable dict.

    `meta` is optional context (e.g. the market regime label, the requested
    universe) folded in alongside the proposals.
    """
    actions = list(actions)
    return {
        "schema": SCHEMA,
        "version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "disclaimer": DISCLAIMER,
        "count": len(actions),
        "actions": [action_to_dict(a) for a in actions],
        "meta": meta or {},
    }


def _neutral_action(payload: dict) -> dict:
    """The `actions` object to deliver when there is no signal to report.

    The registered schema makes every `actions` field required, so an empty
    delivery would `INVALID_DELIVERABLE` → SLA-expire. Instead we deliver a
    well-formed, explicitly non-actionable `watch`/`neutral` object for the
    requested asset, so the buyer gets a definite "no catalyst right now" answer
    (and `count` stays 0). The asset is taken from what the buyer asked for
    (`requirements.assets`), falling back to the covered `universe`.
    """
    meta = payload.get("meta") or {}
    req = meta.get("requirements") or {}
    assets = req.get("assets")
    if isinstance(assets, str):
        assets = [assets]
    horizon = req.get("horizon") or (req.get("horizons") or [None])[0]
    asset = (assets or meta.get("universe") or [None])[0]
    return {
        "asset": asset,
        "signal": "watch",
        "direction": "neutral",
        "confidence": 0.0,
        "score": 0.0,
        "horizon": horizon or "intraday",
        "freshness": None,
        "rationale": (
            f"No catalyst signal for {asset or 'the requested universe'} in the "
            "current window — neutral watch, nothing actionable."
        ),
        "created_at": payload.get("generated_at"),
    }


def flatten_signals(payload: dict) -> dict:
    """Reshape a `catalyst.signals` payload into the **registered Croo service
    schema** — a single top signal, no arrays-of-objects, no deep nesting.

    The Dashboard schema-builder can't express arrays-of-objects, so the service
    was registered with `actions` as ONE flat object (the top signal by
    confidence, `asset` carried as a field), plus top-level `catalysts` (array),
    `layers` (object), and `universe` — with `meta` dissolved to the top level.
    Field names match what was registered (note `freshness`, not
    `freshness_minutes`). Applied only on the Croo delivery path; alert/monitor
    sinks keep the canonical nested `build_payload` list.

    When there are no signals to report, `actions` is a well-formed neutral
    `watch` object (see `_neutral_action`) rather than empty — an empty object
    would fail the schema's required fields and expire the order.
    """
    acts = payload.get("actions", [])
    top = acts[0] if acts else {}           # planner already sorted by confidence
    action_obj = {
        "asset": top.get("asset"),
        "signal": top.get("signal"),
        "direction": top.get("direction"),
        "confidence": top.get("confidence"),
        "score": top.get("score"),
        "horizon": top.get("horizon"),
        "freshness": top.get("freshness_minutes"),
        "rationale": top.get("rationale"),
        "created_at": top.get("created_at"),
    } if top else _neutral_action(payload)

    out = {
        "schema": payload.get("schema"),
        "version": payload.get("version"),
        "generated_at": payload.get("generated_at"),
        "disclaimer": payload.get("disclaimer"),
        "count": len(acts),
        "actions": action_obj,
        "catalysts": list(top.get("catalysts", [])) if top else [],
        "layers": top.get("layers", {}) if top else {},
    }
    out.update(payload.get("meta") or {})   # universe / requirements / mode → top level
    return out


def event_to_dict(post: dict) -> dict:
    """One enriched post → a canonical catalyst-**event** object.

    The raw-catalyst counterpart to `action_to_dict`: a watched catalyst landed on
    a watched asset before (or independent of) any buy/sell proposal. Carries just
    enough to act on or link back — no signal aggregation, no confidence.
    """
    assets = post.get("assets")
    if isinstance(assets, str):
        try:
            assets = json.loads(assets)
        except (json.JSONDecodeError, TypeError):
            assets = []
    text = post.get("text") or ""
    return {
        "uri": post.get("uri"),
        "assets": list(assets or []),
        "catalyst": post.get("catalyst"),
        "sentiment": post.get("sentiment_score"),
        "source": post.get("source"),
        "url": post.get("url"),
        "text": text[:280],
        "indexed_at": post.get("indexed_at"),
    }


def build_event_payload(
    events, *, monitor: str | None = None, generated_at: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Serialise matched catalyst events into the versioned event deliverable.

    Distinct schema (`catalyst.events`) from the action payload, but same shape
    discipline so a sink/webhook/Croo delivery treats it identically. `monitor`
    names the watch that fired it.
    """
    events = list(events)
    return {
        "schema": SCHEMA_EVENTS,
        "version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "monitor": monitor,
        "disclaimer": DISCLAIMER,
        "count": len(events),
        "events": [event_to_dict(e) for e in events],
        "meta": meta or {},
    }


# --- Flattened catalyst.events delivery (the "events" Croo service) ----------
# The Dashboard can't register arrays-of-objects, so the feed ships as an
# array-of-strings (one line per event) plus one structured `lead` object. This
# is the events twin of `flatten_signals`.
_EVENT_SENTINEL = "No notable catalyst events in the current window."
_LEAD_FIELDS = ("asset", "catalyst", "event", "direction", "severity",
                "sentiment", "source", "url", "at")


def event_line(e: dict) -> str:
    """One pipe-delimited feed line: ASSET | catalyst | what | direction | sev | age."""
    return " | ".join(str(e.get(k, "")) for k in
                      ("asset", "catalyst", "event", "direction", "severity", "age"))


def build_events_delivery(events, *, meta: dict | None = None, generated_at: str | None = None) -> dict:
    """Assemble the flat `catalyst.events` deliverable from pre-formatted event
    dicts (each carrying the _LEAD_FIELDS plus `age`).

    `events` is expected already filtered, ranked (most market-moving first), and
    capped by the pipeline. Empty → a well-formed feed with a single sentinel line
    (never an empty array, which the backend would reject as missing)."""
    events = list(events)
    out = {
        "schema": SCHEMA_EVENTS,
        "version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "disclaimer": DISCLAIMER,
        "count": len(events),
        "events": [event_line(e) for e in events] or [_EVENT_SENTINEL],
    }
    if events:
        out["lead"] = {k: events[0].get(k) for k in _LEAD_FIELDS}
    assets = sorted({e["asset"] for e in events if e.get("asset")})
    catalysts = sorted({e["catalyst"] for e in events if e.get("catalyst")})
    if assets:
        out["assets"] = assets
    if catalysts:
        out["catalysts"] = catalysts
    out.update(meta or {})    # window_hours / requirements → top level
    return out


def select_actions(
    actions,
    *,
    assets: list[str] | None = None,
    signals: list[str] | None = None,
    directions: list[str] | None = None,
    horizons: list[str] | None = None,
    min_confidence: float = 0.0,
) -> list:
    """Filter `Action[]` to a buyer's requirements (the Croo order shape).

    Filters on the deliverable's watch vocabulary: `signals` (alert | watch) and
    `directions` (bullish | bearish | neutral) — not buy/sell. All criteria are
    AND-combined; an omitted/empty criterion doesn't filter. Case-insensitive on
    tickers and enum-ish fields. Preserves input ordering (planner sorts by
    confidence).
    """
    asset_set = {a.upper() for a in assets} if assets else None
    signal_set = {s.lower() for s in signals} if signals else None
    dir_set = {d.lower() for d in directions} if directions else None
    horizon_set = {h.lower() for h in horizons} if horizons else None

    out = []
    for a in actions:
        if asset_set is not None and a.asset.upper() not in asset_set:
            continue
        if signal_set is not None and signal_of(a.action) not in signal_set:
            continue
        if dir_set is not None and a.direction.lower() not in dir_set:
            continue
        if horizon_set is not None and a.horizon.lower() not in horizon_set:
            continue
        if a.confidence < min_confidence:
            continue
        out.append(a)
    return out


def requirements_to_kwargs(requirements: dict | None) -> dict:
    """Map a raw buyer `requirements` object to `select_actions` kwargs.

    Accepts the Croo requirements schema (`assets`, `horizon` | `horizons`,
    `signal` | `signals`, `direction` | `directions`, `min_confidence`) and
    tolerates a single string where a list is expected. Unknown keys are ignored
    — a lenient front door for the Phase-5 service contract.
    """
    r = requirements or {}

    def _list(v):
        """Normalize a value into a list. A string is split on commas (the Croo
        Dashboard v2 requirements form can't register an array-of-strings field,
        so buyers send `assets` as a comma-separated string like "BTC,ETH")."""
        if v is None:
            return None
        if isinstance(v, str):
            # strip whitespace AND stray surrounding quotes — the Dashboard string
            # field can arrive double-encoded as `"BTC"` (quotes included).
            parts = [s.strip().strip("\"'").strip() for s in v.split(",")]
            return [p for p in parts if p] or None
        return list(v)

    def _pick(*keys):
        for k in keys:
            if r.get(k) is not None:
                return r.get(k)
        return None

    return {
        "assets": _list(_pick("assets", "asset")),
        "signals": _list(_pick("signal", "signals")),
        "directions": _list(_pick("direction", "directions")),
        "horizons": _list(_pick("horizon", "horizons")),
        "min_confidence": float(r.get("min_confidence", 0.0) or 0.0),
    }


# --- Buyer-selectable lookback window -------------------------------------
# How far back the signal layer reads. Bounded so an order stays sane: at least
# an hour of context, at most a week — matching the richer point-in-time history
# the store now retains (trend adds the multi-day bias slope on top). When a
# buyer requests nothing, callers fall back to DEFAULT_WINDOW_HOURS.
MIN_WINDOW_HOURS = 1.0
MAX_WINDOW_HOURS = 168.0        # 7 days = one week
DEFAULT_WINDOW_HOURS = 24.0

_WINDOW_UNIT_HOURS = {
    "h": 1.0, "hr": 1.0, "hrs": 1.0, "hour": 1.0, "hours": 1.0,
    "d": 24.0, "day": 24.0, "days": 24.0,
    "w": 168.0, "wk": 168.0, "week": 168.0, "weeks": 168.0,
}


def parse_window_hours(value) -> float | None:
    """Parse a lookback window into hours, clamped to [1h, 168h] (a week).

    Accepts a bare number (interpreted as hours) or a string with a unit:
    "6h", "48", "3d", "1w". Returns None when the value is missing or not
    parseable, so the caller can apply its own default. bool is rejected (it
    is an int subclass but never a valid duration)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        hours = float(value)
    else:
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", str(value))
        if not m:
            return None
        unit = (m.group(2) or "h").lower()
        if unit not in _WINDOW_UNIT_HOURS:
            return None
        hours = float(m.group(1)) * _WINDOW_UNIT_HOURS[unit]
    return max(MIN_WINDOW_HOURS, min(MAX_WINDOW_HOURS, hours))


def requirements_window_hours(requirements: dict | None) -> float | None:
    """The buyer-requested signal lookback in hours, or None if unspecified.

    Reads `window` / `lookback` / `window_hours` (each hours-or-unit-string),
    then `window_days` (bare number = days). None means "buyer didn't ask" so
    the pipeline keeps its default — distinct from an unparseable value, which
    also yields None rather than raising (a lenient service front door)."""
    r = requirements or {}
    for key in ("window", "lookback", "window_hours"):
        if r.get(key) is not None:
            return parse_window_hours(r.get(key))
    days = r.get("window_days")
    if days is not None:
        return parse_window_hours(days if isinstance(days, str) else f"{float(days)}d")
    return None
