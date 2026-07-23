"""Learning data layer — the score→outcome dataset a future learned factor trains on.

Each poll cycle this module (1) records every computed signal as a
`score_snapshots` row (features: the Signal fields, the planner's per-layer
bias breakdown, spot price at score time), (2) pre-creates one pending
`score_outcomes` row per horizon, and later — when a horizon has elapsed —
fills in the realized forward return (the label), and (3) logs significant
market moves with the catalysts that were active in the window (or flags them
unexplained).

No scheduler: resolution rides the normal poll cadence. DefiLlama serves
*history*, so a resolution that runs late still lands on the correct
historical price — a missed cycle only delays the write, never corrupts it.
Everything here is called fail-soft from the poll loop; an outage leaves rows
pending for the next cycle.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .prices import PriceOracle
from .store import (
    bump_outcome_attempt,
    fetch_due_outcomes,
    fetch_latest_score,
    fetch_recent_actions,
    fetch_recent_moves,
    resolve_outcome,
    save_market_move,
    save_pending_outcomes,
    save_score_snapshot,
)

DEFAULT_LEARNING: dict = {
    "enabled": True,
    "horizons_hours": [1.0, 24.0, 72.0],   # 24h is the headline label
    "price_period": "1h",
    "price_tolerance_hours": 3.0,
    "resolve_batch": 500,
    "give_up_hours": 48.0,                 # pending past due+this with no price → no_price
    "moves": {
        "assets": ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BNB", "AVAX", "LINK"],
        "window_hours": 24.0,
        "threshold": 0.05,                 # |return| that counts as a significant move
        "cooldown_hours": 12.0,            # don't re-record the same drift every cycle
        "attribution_slack_hours": 6.0,    # news slightly older than the window still explains it
    },
}


def learning_cfg(config_path: str) -> dict:
    """The config's `learning` block over the defaults (moves merged one level deep)."""
    try:
        raw = json.loads(Path(config_path).read_text(encoding="utf-8")).get("learning") or {}
    except Exception:  # noqa: BLE001 — absent/bad config file just means defaults
        raw = {}
    cfg = {**DEFAULT_LEARNING, **raw}
    cfg["moves"] = {**DEFAULT_LEARNING["moves"], **(raw.get("moves") or {})}
    return cfg


def build_oracle(conn, cfg: dict, assets, now: datetime) -> PriceOracle | None:
    """One price fetch covering everything this cycle needs, or None on failure.

    Universe = this cycle's signal assets ∪ assets with due outcomes ∪ the
    move-watch list ∪ BTC (the baseline). Window reaches back far enough to
    back-fill entry prices for the oldest row still worth resolving."""
    try:
        universe = {a.upper() for a in assets}
        universe.update(a.upper() for a in cfg["moves"]["assets"])
        universe.add("BTC")
        for o in fetch_due_outcomes(conn, now_iso=now.isoformat(),
                                    limit=cfg.get("resolve_batch", 500)):
            universe.add(o["asset"].upper())
        lookback = max(cfg["horizons_hours"]) + cfg["give_up_hours"] + 24.0
        oracle = PriceOracle.fetch(
            universe, now - timedelta(hours=lookback), now,
            period=cfg.get("price_period", "1h"),
        )
        oracle.tolerance = int(cfg.get("price_tolerance_hours", 3.0) * 3600)
        return oracle
    except Exception:  # noqa: BLE001 — no prices this cycle; rows stay pending
        return None


def record_cycle(
    conn, *, ts: str, signals, actions, horizons, cycle: int | None = None,
    oracle: PriceOracle | None = None,
) -> int:
    """Persist this cycle's signals as snapshots + pending outcomes. Returns count.

    Records ALL signals, not just the ones that became actions — sub-threshold
    scores are exactly the negatives a learner needs. Idempotent per (ts, asset)."""
    created_at = datetime.now(timezone.utc).isoformat()
    by_asset = {a.asset: a for a in (actions or [])}
    n = 0
    for s in signals or []:
        act = by_asset.get(s.asset)
        row = {
            "cycle": cycle,
            "ts": ts,
            "asset": s.asset,
            "sentiment": s.sentiment,
            "strength": s.strength,
            "score": s.score,
            "direction": s.direction,
            "mentions": s.mentions,
            "velocity": s.velocity,
            "catalysts": json.dumps(s.catalysts),
            "sources": json.dumps(s.sources),
            "latest_at": s.latest_at,
            "action": act.action if act else None,
            "confidence": act.confidence if act else None,
            "horizon": act.horizon if act else None,
            "layers": json.dumps(act.layers) if act else None,
            "price_at_score": oracle.price_at(s.asset, ts) if oracle else None,
            "created_at": created_at,
        }
        snapshot_id = save_score_snapshot(conn, row)
        save_pending_outcomes(conn, snapshot_id, s.asset, ts, horizons)
        n += 1
    return n


def resolve_due(conn, *, now: datetime, oracle: PriceOracle, cfg: dict) -> dict:
    """Fill in realized returns for every pending outcome whose horizon elapsed.

    Entry price prefers what was captured at score time and back-fills from
    history otherwise; a row that still has no price after `give_up_hours` past
    due is retired as no_price so the due-queue can't grow unbounded."""
    counts = {"resolved": 0, "gave_up": 0, "pending": 0}
    resolved_at = now.isoformat()
    give_up_after = timedelta(hours=cfg.get("give_up_hours", 48.0))
    for o in fetch_due_outcomes(conn, now_iso=resolved_at,
                                limit=cfg.get("resolve_batch", 500)):
        entry = o["entry_px"] or o["price_at_score"] or oracle.price_at(o["asset"], o["scored_at"])
        exit_ = oracle.price_at(o["asset"], o["due_at"])
        if entry and exit_:
            btc0 = oracle.price_at("BTC", o["scored_at"])
            btc1 = oracle.price_at("BTC", o["due_at"])
            btc_ret = (btc1 / btc0 - 1.0) if (btc0 and btc1) else None
            resolve_outcome(
                conn, o["id"], entry_px=entry, exit_px=exit_,
                ret=exit_ / entry - 1.0, btc_ret=btc_ret, resolved_at=resolved_at,
            )
            counts["resolved"] += 1
        else:
            give_up = now > datetime.fromisoformat(o["due_at"]) + give_up_after
            bump_outcome_attempt(conn, o["id"], give_up=give_up)
            counts["gave_up" if give_up else "pending"] += 1
    return counts


def detect_moves(conn, *, now: datetime, oracle: PriceOracle, enriched_rows, cfg: dict) -> int:
    """Record significant window moves on the watch assets, with attribution.

    A move is `explained` when catalyst-tagged news or a buy/sell action covered
    the window; unexplained moves are the more interesting label — catalysts the
    pipeline missed entirely."""
    mcfg = cfg["moves"]
    window = float(mcfg["window_hours"])
    detected_at = now.isoformat()
    slack = float(mcfg.get("attribution_slack_hours", 6.0))
    attrib_cutoff = (now - timedelta(hours=window + slack)).isoformat()
    n = 0
    for asset in mcfg["assets"]:
        asset = asset.upper()
        p_now = oracle.price_at(asset, now)
        p_then = oracle.price_at(asset, now - timedelta(hours=window))
        if not p_now or not p_then:
            continue
        ret = p_now / p_then - 1.0
        if abs(ret) < float(mcfg["threshold"]):
            continue
        # Cooldown: the same drift shouldn't re-record every 15 minutes.
        recent = fetch_recent_moves(conn, asset=asset,
                                    within_hours=float(mcfg["cooldown_hours"]), now=now)
        if any((m["ret"] or 0) * ret > 0 for m in recent):
            continue

        catalysts: list[str] = []
        evidence: list[dict] = []
        for r in enriched_rows or []:
            try:
                row_assets = json.loads(r.get("assets") or "[]")
            except (TypeError, json.JSONDecodeError):
                row_assets = []
            if asset not in row_assets:
                continue
            if (r.get("indexed_at") or "") < attrib_cutoff:
                continue
            if r.get("catalyst") and r["catalyst"] not in catalysts:
                catalysts.append(r["catalyst"])
            if len(evidence) < 10:
                evidence.append({
                    "uri": r.get("uri"), "catalyst": r.get("catalyst"),
                    "event": r.get("event"), "sentiment_score": r.get("sentiment_score"),
                })
        trades = [a for a in fetch_recent_actions(conn, within_minutes=window * 60, now=now)
                  if a["asset"] == asset and a["action"] in ("buy", "sell")]
        save_market_move(conn, {
            "asset": asset,
            "detected_at": detected_at,
            "window_hours": window,
            "start_px": p_then,
            "end_px": p_now,
            "ret": ret,
            "catalysts": json.dumps(catalysts),
            "evidence": json.dumps(evidence),
            "signal_score": fetch_latest_score(conn, asset, since_iso=attrib_cutoff),
            "action": trades[0]["action"] if trades else None,
            "explained": 1 if (catalysts or trades) else 0,
        })
        n += 1
    return n
