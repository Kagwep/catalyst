"""Trend layer — turn accumulated `bias_snapshots` history into a per-asset
*direction of travel* over days.

Every other layer (macro/flows/supply/market/derivs) answers "where is this asset's
bias *now*". The trend layer answers "which way has it been *moving*" — the slope of
a layer's bias across a multi-day window. A flows bias that has been climbing for
days (institutional accumulation strengthening) is a different, stronger setup than
a flat one at the same level.

v1 trends the **flows** layer (multi-day accumulation/distribution is the canonical
multi-day signal); the window/layer are parameters so it generalizes later.

The output is a `TrendBias` with the SAME `(asset, bias, label, evidence)` shape the
other layers expose, so it slots into `planner.plan`'s modifier loop as one more
entry — no new apply path. Sign convention matches the others: a **positive** trend
bias = the asset's bias is moving *more bullish* (a rising flows/accumulation trend),
so it boosts an aligned buy exactly like the point-in-time layers.

Point-in-time safe: reads only snapshots at `ts <= now`, so it replays correctly in
the backtest (no lookahead).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .store import fetch_bias_snapshots


@dataclass
class TrendBias:
    asset: str
    bias: float              # -1 (falling/weakening) .. +1 (rising/strengthening)
    label: str               # strengthening | flat | weakening
    evidence: float          # number of snapshots behind the slope
    drivers: list[str] = field(default_factory=list)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _slope_drift(points: list[tuple[float, float]]) -> float:
    """OLS slope of y vs x (days), returned as the drift over the observed span
    (slope × span) — i.e. how much the bias moved across the window, in bias units.

    Using the fitted slope (not raw last−first) is robust to single-cycle noise;
    scaling by the span puts the result on the same [-1, 1] scale as the inputs.
    """
    n = len(points)
    if n < 2:
        return 0.0
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    if sxx == 0:                      # all snapshots at the same instant
        return 0.0
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    slope = sxy / sxx                # bias units per day
    span = points[-1][0] - points[0][0]
    return slope * span


def compute_trend_bias(
    conn,
    assets,
    *,
    layer: str = "flows",
    window_days: float = 7.0,
    now: datetime | str | None = None,
    min_points: int = 3,
    flat_threshold: float = 0.1,
) -> dict[str, TrendBias]:
    """Per-asset trend of `layer`'s bias over the trailing `window_days`.

    Reads `bias_snapshots` at `ts <= now`, fits the slope of each asset's series,
    and returns `{asset: TrendBias}` for assets with enough history. An asset with
    fewer than `min_points` snapshots in the window is **omitted** (thin/cold-start
    history yields no modifier, never a spurious `flat`).
    """
    if now is None:
        now_dt = datetime.now(timezone.utc)
    elif isinstance(now, str):
        now_dt = _parse_dt(now) or datetime.now(timezone.utc)
    else:
        now_dt = now
    now_iso = now_dt.isoformat()
    window_start = now_dt - timedelta(days=window_days)

    out: dict[str, TrendBias] = {}
    for asset in {a for a in assets}:
        rows = fetch_bias_snapshots(conn, layer=layer, asset=asset, before=now_iso)
        series: list[tuple[float, float]] = []
        for r in rows:
            ts = _parse_dt(r.get("ts"))
            if ts is None or ts < window_start:
                continue
            series.append((ts, float(r.get("bias") or 0.0)))
        if len(series) < min_points:
            continue
        t0 = series[0][0]
        points = [((ts - t0).total_seconds() / 86400.0, b) for ts, b in series]
        drift = _clamp(_slope_drift(points))
        label = ("strengthening" if drift >= flat_threshold
                 else "weakening" if drift <= -flat_threshold else "flat")
        out[asset] = TrendBias(asset=asset, bias=round(drift, 3), label=label,
                               evidence=float(len(series)))
    return out
